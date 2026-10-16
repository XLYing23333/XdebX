"""Production-host probes remain bounded and cannot write files."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from test_core import context
from xdebx.runtime import Runner, InstallError
from xdebx.resources import Resources
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import probe_urls


class Response:
    def __init__(self, body=b'package', status=200, content_type='application/octet-stream'):
        self.status=status;self.url='https://example.invalid/package'
        self.headers={'Content-Type':content_type,'Content-Length':str(len(body))}
        self.stream=io.BytesIO(body)
    def __enter__(self):return self
    def __exit__(self,*_):self.stream.close()
    def read1(self,size):return self.stream.read(size)


class ProbeTests(unittest.TestCase):
    item={'url':'https://example.invalid/package','label':'test','mode':'CN','kind':'binary','effective':True}
    def test_head_unsupported_and_range_ignored_still_reads_only_64k(self):
        calls=[]
        class Opener:
            def open(_,request,timeout):
                calls.append((request.get_method(),request.headers,timeout))
                if request.get_method()=='HEAD':
                    raise urllib.error.HTTPError(request.full_url,405,'unsupported',{},None)
                return Response(b'x'*(probe_urls.CAP*4))
        result=probe_urls.probe(self.item,Opener(),probe_urls.BUDGET)
        self.assertEqual(result['status'],'reachable-sample-only',result)
        self.assertEqual(result['bytes_read'],probe_urls.CAP)
        self.assertFalse(result['attempts'][0]['range_honored'])
        self.assertEqual([c[0] for c in calls],['HEAD','GET'])
        self.assertEqual(calls[1][1]['Range'],'bytes=0-65535')

    def test_html_200_is_failure_and_total_budget_is_honored(self):
        class Opener:
            def open(_,request,timeout):return Response(b'<html>'+b'x'*probe_urls.CAP,content_type='text/html')
        result=probe_urls.probe(self.item,Opener(),100)
        self.assertNotEqual(result['status'],'reachable-sample-only')
        self.assertEqual(result['bytes_read'],100)
        self.assertIn('HTML',result['attempts'][0]['error'])

    def test_worker_does_not_write_and_marks_remaining_urls_untested(self):
        class Opener:
            def open(_,request,timeout):return Response(b'x'*probe_urls.CAP)
        output=io.StringIO()
        with patch.object(probe_urls,'BUDGET',100),patch.object(probe_urls.urllib.request,'build_opener',return_value=Opener()),patch.object(Path,'write_text',side_effect=AssertionError('remote write')),contextlib.redirect_stdout(output):
            probe_urls.worker([self.item,self.item])
        results=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(results[0]['bytes_read'],100)
        self.assertEqual(results[1]['status'],'not-tested-budget')
        self.assertEqual(results[-1]['remote_files_created'],0)

    def test_cn_failure_retries_original_without_changing_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=context(Path(tmp));ctx.settings['mirror']='CN';logs=[]
            resources=Resources(ctx,Runner(ctx,logs.append));calls=[]
            def fetch(url,*_):
                calls.append(url)
                if 'npmmirror' in url:raise InstallError('mirror unavailable')
                return Path(tmp)/'success'
            with patch.object(resources,'_fetch',side_effect=fetch):
                self.assertEqual(resources.fetch('https://nodejs.org/dist/v22/node.tar.xz',Path(tmp)/'download',{}),Path(tmp)/'success')
            self.assertEqual(len(calls),2);self.assertIn('npmmirror',calls[0]);self.assertIn('nodejs.org',calls[1])
            self.assertEqual(ctx.settings['mirror'],'CN')
