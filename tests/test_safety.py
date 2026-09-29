"""Offline regression tests. Any attempt to open a network connection fails."""
import asyncio
import io
import os
import socket
import tempfile
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app import config, database, download_service as downloads, main, query_service
from app.models import DownloadRequest, MetadataFilters, MetadataSearchRequest, SearchRequest
from app.metadata_service import has_any_filter


def settings():
    return config.Settings(
        environments={name: config.DBEnvironment(name, name + '_user', 'fake',
                       name + '.invalid', 3306, True, name + '-bucket')
                      for name in ('production', 'staging')},
        common_db=config.CommonDB('common', 'fake', 'common.invalid', 3306, 'common'),
        max_search_rows=5000, max_download_files=2500,
    )


def rows(count=1):
    return [dict(id=str(i), account_number='A', encounter_id=i, filePath=f'{i}.pdf',
                 facility_code='F', worktype='NOTE', document_id=i)
            for i in range(count)]


class ChunkBody(io.BytesIO):
    def read(self, size=-1):
        if not 0 < size <= downloads.CHUNK_SIZE:
            raise AssertionError('Unbounded read')
        return super().read(size)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.cfg = settings()
        self.patches = [patch.object(module, 'get_settings', return_value=self.cfg)
                        for module in (main, downloads, database, query_service)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(database.get_client_engine.cache_clear)
        self.addCleanup(database.get_engine.cache_clear)
        self.jobs = patch.dict(main._jobs, {}, clear=True)
        self.jobs.start()
        self.addCleanup(self.jobs.stop)

    def test_blank_filters_rejected_before_engine_lookup(self):
        for cls, handler in ((SearchRequest, main.search), (DownloadRequest, main.download_start)):
            for field in ('account_numbers', 'encounter_ids'):
                with self.subTest(cls=cls, field=field), patch.object(main, '_engine_for') as engine:
                    request = cls(client='DEMO', selected_files=['filePath'], **{field: ['', '  ', '\t']})
                    with self.assertRaises(HTTPException) as error:
                        handler(request)
                    self.assertEqual(error.exception.status_code, 400)
                    engine.assert_not_called()

    def test_normalization_and_either_or(self):
        request = SearchRequest(client='DEMO', account_numbers=[' A ', '', 'A'], encounter_ids=[' '])
        main._require_one_direct_filter(request)
        self.assertEqual(request.account_numbers, ['A'])
        self.assertEqual(request.encounter_ids, [])
        request.encounter_ids = ['1']
        with self.assertRaises(HTTPException):
            main._require_one_direct_filter(request)
        self.assertFalse(has_any_filter(MetadataFilters(client_codes=[' '], encounter_ids=[''])))

    def test_query_layer_rejects_blank_without_connecting(self):
        engine = Mock()
        with self.assertRaises(ValueError):
            query_service.search_documents(engine, SearchRequest(client='DEMO', account_numbers=[' ']))
        engine.connect.assert_not_called()

    def test_environment_database_routing_without_connections(self):
        database.get_client_engine.cache_clear()
        database.get_engine.cache_clear()
        with patch.object(database, 'create_engine') as create:
            for environment in ('production', 'staging'):
                database.get_engine(environment)
                url = create.call_args.args[0]
                self.assertEqual(url.host, environment + '.invalid')
                self.assertEqual(url.username, environment + '_user')
                database.get_client_engine(environment, 'DEMO')
                url = create.call_args.args[0]
                self.assertEqual(url.host, environment + '.invalid')
                self.assertEqual(url.database, 'CAPC_APIGATEWAY_DEMO')

    def test_environment_config_prefixes_and_limits(self):
        values = {'COMMON_DB_USER': 'common', 'COMMON_DB_HOST': 'common.invalid',
                  'COMMON_DB_NAME': 'common', 'MAX_DOWNLOAD_BYTES': '1234'}
        for prefix in ('PROD', 'STAGING'):
            for suffix in ('DB_USER', 'DB_PASSWORD', 'DB_HOST', 'S3_BUCKET'):
                values[f'{prefix}_{suffix}'] = f'{prefix}_{suffix}_fake'
        with patch.dict(os.environ, values, clear=True):
            loaded = config.get_settings()
            self.assertEqual(loaded.environment('production').db_user, 'PROD_DB_USER_fake')
            self.assertEqual(loaded.environment('staging').s3_bucket, 'STAGING_S3_BUCKET_fake')
            self.assertEqual(loaded.max_download_bytes, 1234)
            os.environ['MAX_DOWNLOAD_JOBS'] = '0'
            with self.assertRaises(ValueError):
                config.get_settings()

    def test_metadata_uses_selected_webdb_environment(self):
        for environment in ('production', 'staging'):
            request = MetadataSearchRequest(environment=environment,
                filters=MetadataFilters(client_codes=['DEMO']), selected_files=['filePath'])
            with patch.object(main, 'get_common_engine'), \
                 patch.object(main, 'search_overall_data', return_value=[{'client_code': 'DEMO', 'encounter_id': 1}]), \
                 patch.object(main, 'list_client_codes', return_value=('DEMO',)) as clients, \
                 patch.object(main, 'get_client_engine') as engine, \
                 patch.object(main, 'search_documents', return_value=rows()) as search:
                main._resolve_via_common_db(request)
                clients.assert_called_once_with(environment)
                engine.assert_called_once_with(environment, 'DEMO')
                self.assertEqual(search.call_args.args[1].environment, environment)

    def test_both_buckets_stream_to_disk_and_close_bodies(self):
        for environment in ('production', 'staging'):
            bodies = []
            client = Mock()
            def get_object(**kwargs):
                self.assertEqual(kwargs['Bucket'], environment + '-bucket')
                body = ChunkBody(b'content' * 200000)
                bodies.append(body)
                return {'Body': body}
            client.get_object.side_effect = get_object
            progress = []
            with patch.object(downloads.boto3, 'client', return_value=client):
                result = downloads.zip_documents(rows(10), ['filePath'], environment=environment,
                                                 progress_callback=progress.append)
            try:
                self.assertIsInstance(result.path, Path)
                self.assertEqual(result.file_count, 10)
                with zipfile.ZipFile(result.path) as archive:
                    self.assertEqual(len(archive.namelist()), 10)
                    self.assertEqual(archive.read('files/A_0_F_NOTE_0.txt'), b'content' * 200000)
                self.assertTrue(all(body.closed for body in bodies))
                self.assertEqual(progress[-1].files_done, 10)
                client.close.assert_called_once()
            finally:
                result.cleanup()
            self.assertFalse(result.path.exists())

    def test_all_files_folder_preserves_100_encounters_and_name_collisions(self):
        request = DownloadRequest(client='DEMO', selected_files=['filePath'])
        documents = [dict(id=str(i), account_number='A', encounter_id=i,
                          filePath='shared/document.txt', facility_code='F',
                          worktype='NOTE', document_id=i) for i in range(100)]
        # Repeated document rows within an encounter still download only once.
        documents.append(dict(documents[0]))
        client = Mock()
        client.get_object.side_effect = lambda **kwargs: {'Body': ChunkBody(b'original')}
        self.assertEqual(downloads.download_totals(documents, request.selected_files,
                                                  request.folder_structure), (100, 100))
        with patch.object(downloads.boto3, 'client', return_value=client):
            result = downloads.zip_documents(documents, request.selected_files,
                                             request.folder_structure)
        try:
            self.assertEqual(result.file_count, 100)
            with zipfile.ZipFile(result.path) as archive:
                names = archive.namelist()
                self.assertEqual(len(set(names)), 100)
                self.assertEqual({str(Path(name).parent) for name in names}, {'files'})
                self.assertIn('files/A_0_F_NOTE_0.txt', names)
                self.assertIn('files/A_99_F_NOTE_99.txt', names)
                self.assertTrue(all(archive.read(name) == b'original' for name in names))
        finally:
            result.cleanup()

    def test_original_names_for_multiple_documents_and_other_file_types(self):
        documents = [dict(account_number='ACC', encounter_id=42, facility_code='FAC',
                          worktype='NOTE', document_id=i, filePath=f'doc/{i}.xml',
                          orignal_path=f'pdf/{i}.pdf') for i in (10, 11)]
        for structure in ('single_folder', 'facility_wise'):
            items = downloads.collect_download_items(
                documents, ['filePath', 'orignal_path'], 'bucket', structure)
            self.assertEqual([item.filename for item in items],
                             ['ACC_42_FAC_NOTE_10.txt', '10.pdf',
                              'ACC_42_FAC_NOTE_11.txt', '11.pdf'])
            self.assertEqual(items[0].key, 'ezcapc/doc/10.xml')

    def test_byte_limit_fails_and_removes_temporary_files(self):
        cfg = replace(self.cfg, max_download_bytes=2)
        body = ChunkBody(b'too large')
        client = Mock()
        client.get_object.return_value = {'Body': body}
        with tempfile.TemporaryDirectory() as parent:
            factory = tempfile.TemporaryDirectory
            with patch.object(downloads, 'get_settings', return_value=cfg), \
                 patch.object(downloads.boto3, 'client', return_value=client), \
                 patch.object(downloads.tempfile, 'TemporaryDirectory',
                              side_effect=lambda **kw: factory(dir=parent, **kw)):
                with self.assertRaises(downloads.DownloadLimitError):
                    downloads.zip_documents(rows(), ['filePath'])
            self.assertEqual(list(Path(parent).iterdir()), [])
        self.assertTrue(body.closed)

    def test_failed_and_capped_files_reported(self):
        client = Mock()
        client.get_object.side_effect = RuntimeError('missing')
        with patch.object(downloads, 'get_settings', return_value=replace(self.cfg, max_download_files=1)), \
             patch.object(downloads.boto3, 'client', return_value=client):
            result = downloads.zip_documents(rows(2), ['filePath'])
        try:
            self.assertEqual((result.file_count, result.skipped_count), (0, 2))
            client.get_object.assert_called_once()
            with zipfile.ZipFile(result.path) as archive:
                self.assertIn('FAILED_A_0_F_NOTE_0.txt.txt', archive.namelist()[0])
        finally:
            result.cleanup()

    def make_job(self):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / 'test.zip'
        path.write_bytes(b'test archive')
        archive = downloads.ZipResult(path, directory, 1, 0)
        self.addCleanup(archive.cleanup)
        job = main.DownloadJob(status='done', archive=archive, expires_at=time.monotonic() + 100)
        main._jobs['test'] = job
        return job

    def test_file_response_streams_and_cleans_after_delivery(self):
        job = self.make_job()
        response = main.download_file('test')
        self.assertTrue(job.archive.path.exists())
        sent = []
        async def send(message): sent.append(message)
        asyncio.run(response({'type': 'http', 'method': 'GET', 'headers': []}, None, send))
        self.assertEqual(b''.join(m.get('body', b'') for m in sent), b'test archive')
        self.assertNotIn('test', main._jobs)
        self.assertFalse(job.archive.path.exists())

    def test_failed_transfer_remains_retryable_and_expires(self):
        job = self.make_job()
        response = main.download_file('test')
        async def send(_message): raise OSError('disconnected')
        with self.assertRaises(OSError):
            asyncio.run(response({'type': 'http', 'method': 'GET', 'headers': []}, None, send))
        self.assertEqual(job.active_transfers, 0)
        self.assertIn('test', main._jobs)
        job.expires_at = 0
        main._cleanup_expired_jobs()
        self.assertNotIn('test', main._jobs)
        self.assertFalse(job.archive.path.exists())

    def test_active_transfers_protected_from_expiry(self):
        job = self.make_job()
        job.expires_at = 0
        job.active_transfers = 1
        main._cleanup_expired_jobs()
        self.assertIn('test', main._jobs)
        self.assertTrue(job.archive.path.exists())

    def test_partial_download_does_not_delete_archive(self):
        job = self.make_job()
        response = main.download_file('test')
        sent = []
        async def send(message): sent.append(message)
        asyncio.run(response({'type': 'http', 'method': 'GET',
                              'headers': [(b'range', b'bytes=0-3')]}, None, send))
        self.assertEqual(sent[0]['status'], 206)
        self.assertTrue(job.archive.path.exists())
        self.assertEqual(job.active_transfers, 0)

    def test_completed_job_owns_disk_archive_and_expires(self):
        job = self.make_job()
        archive = job.archive
        job.archive = None
        job.status = 'running'
        request = DownloadRequest(client='DEMO', encounter_ids=['1'], selected_files=['filePath'])
        with patch.object(main, 'zip_documents', return_value=archive):
            main._run_download_job('test', job, request, rows())
        self.assertEqual(job.status, 'done')
        self.assertIs(job.archive, archive)
        self.assertGreater(job.expires_at, time.monotonic())
        job.expires_at = 0
        main._cleanup_expired_jobs()
        self.assertFalse(archive.path.exists())

    def test_capacity_rejects_before_starting_thread(self):
        main._jobs.update({'a': main.DownloadJob(), 'b': main.DownloadJob()})
        request = DownloadRequest(client='DEMO', encounter_ids=['1'], selected_files=['filePath'])
        with patch.object(main.threading, 'Thread') as thread:
            with self.assertRaises(HTTPException) as error:
                main._start_download_job(request, rows())
            self.assertEqual(error.exception.status_code, 429)
            thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
