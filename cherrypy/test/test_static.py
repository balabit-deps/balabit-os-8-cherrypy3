# -*- coding: utf-8 -*-
import contextlib
import io
import os
import sys
import platform

from six.moves import urllib

import pytest

import cherrypy
from cherrypy.lib import static
from cherrypy._cpcompat import (
    HTTPConnection, HTTPSConnection, ntou, tonative,
)
from cherrypy.test import helper


curdir = os.path.join(os.getcwd(), os.path.dirname(__file__))
has_space_filepath = os.path.join(curdir, 'static', 'has space.html')
bigfile_filepath = os.path.join(curdir, 'static', 'bigfile.log')

# The file size needs to be big enough such that half the size of it
# won't be socket-buffered (or server-buffered) all in one go. See
# test_file_stream.
MB = 2 ** 20
BIGFILE_SIZE = 32 * MB


class StaticTest(helper.CPWebCase):

    @staticmethod
    def setup_server():
        if not os.path.exists(has_space_filepath):
            with open(has_space_filepath, 'wb') as f:
                f.write(b'Hello, world\r\n')
        needs_bigfile = (
            not os.path.exists(bigfile_filepath)
            or os.path.getsize(bigfile_filepath) != BIGFILE_SIZE
        )
        if needs_bigfile:
            with open(bigfile_filepath, 'wb') as f:
                f.write(b'x' * BIGFILE_SIZE)

        class Root:

            @cherrypy.expose
            @cherrypy.config(**{'response.stream': True})
            def bigfile(self):
                self.f = static.serve_file(bigfile_filepath)
                return self.f

            @cherrypy.expose
            def tell(self):
                if self.f.input.closed:
                    return ''
                return repr(self.f.input.tell()).rstrip('L')

            @cherrypy.expose
            def fileobj(self):
                f = open(os.path.join(curdir, 'style.css'), 'rb')
                return static.serve_fileobj(f, content_type='text/css')

            @cherrypy.expose
            def bytesio(self):
                f = io.BytesIO(b'Fee\nfie\nfo\nfum')
                return static.serve_fileobj(f, content_type='text/plain')

        class Static:

            @cherrypy.expose
            def index(self):
                return 'You want the Baron? You can have the Baron!'

            @cherrypy.expose
            def dynamic(self):
                return 'This is a DYNAMIC page'

        root = Root()
        root.static = Static()

        rootconf = {
            '/static': {
                'tools.staticdir.on': True,
                'tools.staticdir.dir': 'static',
                'tools.staticdir.root': curdir,
            },
            '/style.css': {
                'tools.staticfile.on': True,
                'tools.staticfile.filename': os.path.join(curdir, 'style.css'),
            },
            '/docroot': {
                'tools.staticdir.on': True,
                'tools.staticdir.root': curdir,
                'tools.staticdir.dir': 'static',
                'tools.staticdir.index': 'index.html',
            },
            '/error': {
                'tools.staticdir.on': True,
                'request.show_tracebacks': True,
            },
            '/404test': {
                'tools.staticdir.on': True,
                'tools.staticdir.root': curdir,
                'tools.staticdir.dir': 'static',
                'error_page.404': error_page_404,
            }
        }
        rootApp = cherrypy.Application(root)
        rootApp.merge(rootconf)

        test_app_conf = {
            '/test': {
                'tools.staticdir.index': 'index.html',
                'tools.staticdir.on': True,
                'tools.staticdir.root': curdir,
                'tools.staticdir.dir': 'static',
            },
        }
        testApp = cherrypy.Application(Static())
        testApp.merge(test_app_conf)

        vhost = cherrypy._cpwsgi.VirtualHost(rootApp, {'virt.net': testApp})
        cherrypy.tree.graft(vhost)

    @staticmethod
    def teardown_server():
        for f in (has_space_filepath, bigfile_filepath):
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except:
                    pass

    def test_static(self):
        self.getPage('/static/index.html')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/html')
        self.assertBody('Hello, world\r\n')

        # Using a staticdir.root value in a subdir...
        self.getPage('/docroot/index.html')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/html')
        self.assertBody('Hello, world\r\n')

        # Check a filename with spaces in it
        self.getPage('/static/has%20space.html')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/html')
        self.assertBody('Hello, world\r\n')

        self.getPage('/style.css')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/css')
        # Note: The body should be exactly 'Dummy stylesheet\n', but
        #   unfortunately some tools such as WinZip sometimes turn \n
        #   into \r\n on Windows when extracting the CherryPy tarball so
        #   we just check the content
        self.assertMatchesBody('^Dummy stylesheet')

    def test_fallthrough(self):
        # Test that NotFound will then try dynamic handlers (see [878]).
        self.getPage('/static/dynamic')
        self.assertBody('This is a DYNAMIC page')

        # Check a directory via fall-through to dynamic handler.
        self.getPage('/static/')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/html;charset=utf-8')
        self.assertBody('You want the Baron? You can have the Baron!')

    def test_index(self):
        # Check a directory via "staticdir.index".
        self.getPage('/docroot/')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/html')
        self.assertBody('Hello, world\r\n')
        # The same page should be returned even if redirected.
        self.getPage('/docroot')
        self.assertStatus(301)
        self.assertHeader('Location', '%s/docroot/' % self.base())
        self.assertMatchesBody("This resource .* <a href=(['\"])%s/docroot/\\1>"
                               '%s/docroot/</a>.' % (self.base(), self.base()))

    def test_config_errors(self):
        # Check that we get an error if no .file or .dir
        self.getPage('/error/thing.html')
        self.assertErrorPage(500)
        if sys.version_info >= (3, 3):
            errmsg = (
                'TypeError: staticdir\(\) missing 2 '
                'required positional arguments'
            )
        else:
            errmsg = (
                'TypeError: staticdir\(\) takes at least 2 '
                '(positional )?arguments \(0 given\)'
            )
        self.assertMatchesBody(errmsg.encode('ascii'))

    def test_security(self):
        # Test up-level security
        self.getPage('/static/../../test/style.css')
        self.assertStatus((400, 403))

    def test_modif(self):
        # Test modified-since on a reasonably-large file
        self.getPage('/static/dirback.jpg')
        self.assertStatus('200 OK')
        lastmod = ''
        for k, v in self.headers:
            if k == 'Last-Modified':
                lastmod = v
        ims = ('If-Modified-Since', lastmod)
        self.getPage('/static/dirback.jpg', headers=[ims])
        self.assertStatus(304)
        self.assertNoHeader('Content-Type')
        self.assertNoHeader('Content-Length')
        self.assertNoHeader('Content-Disposition')
        self.assertBody('')

    def test_755_vhost(self):
        self.getPage('/test/', [('Host', 'virt.net')])
        self.assertStatus(200)
        self.getPage('/test', [('Host', 'virt.net')])
        self.assertStatus(301)
        self.assertHeader('Location', self.scheme + '://virt.net/test/')

    def test_serve_fileobj(self):
        self.getPage('/fileobj')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/css;charset=utf-8')
        self.assertMatchesBody('^Dummy stylesheet')

    def test_serve_bytesio(self):
        self.getPage('/bytesio')
        self.assertStatus('200 OK')
        self.assertHeader('Content-Type', 'text/plain;charset=utf-8')
        self.assertHeader('Content-Length', 14)
        self.assertMatchesBody('Fee\nfie\nfo\nfum')

    @pytest.mark.xfail(reason='#1475')
    def test_file_stream(self):
        if cherrypy.server.protocol_version != 'HTTP/1.1':
            return self.skip()

        self.PROTOCOL = 'HTTP/1.1'

        # Make an initial request
        self.persistent = True
        conn = self.HTTP_CONN
        conn.putrequest('GET', '/bigfile', skip_host=True)
        conn.putheader('Host', self.HOST)
        conn.endheaders()
        response = conn.response_class(conn.sock, method='GET')
        response.begin()
        self.assertEqual(response.status, 200)

        body = b''
        remaining = BIGFILE_SIZE
        while remaining > 0:
            data = response.fp.read(65536)
            if not data:
                break
            body += data
            remaining -= len(data)

            if self.scheme == 'https':
                newconn = HTTPSConnection
            else:
                newconn = HTTPConnection
            s, h, b = helper.webtest.openURL(
                b'/tell', headers=[], host=self.HOST, port=self.PORT,
                http_conn=newconn)
            if not b:
                # The file was closed on the server.
                tell_position = BIGFILE_SIZE
            else:
                tell_position = int(b)

            read_so_far = len(body)

            # It is difficult for us to force the server to only read
            # the bytes that we ask for - there are going to be buffers
            # inbetween.
            #
            # CherryPy will attempt to write as much data as it can to
            # the socket, and we don't have a way to determine what that
            # size will be. So we make the following assumption - by
            # the time we have read in the entire file on the server,
            # we will have at least received half of it. If this is not
            # the case, then this is an indicator that either:
            #   - machines that are running this test are using buffer
            #     sizes greater than half of BIGFILE_SIZE; or
            #   - streaming is broken.
            #
            # At the time of writing, we seem to have encountered
            # buffer sizes bigger than 512K, so we've increased
            # BIGFILE_SIZE to 4MB and in 2016 to 20MB and then 32MB.
            # This test is going to keep failing according to the
            # improvements in hardware and OS buffers.
            if tell_position >= BIGFILE_SIZE:
                if read_so_far < (BIGFILE_SIZE / 2):
                    self.fail(
                        'The file should have advanced to position %r, but '
                        'has already advanced to the end of the file. It '
                        'may not be streamed as intended, or at the wrong '
                        'chunk size (64k)' % read_so_far)
            elif tell_position < read_so_far:
                self.fail(
                    'The file should have advanced to position %r, but has '
                    'only advanced to position %r. It may not be streamed '
                    'as intended, or at the wrong chunk size (64k)' %
                    (read_so_far, tell_position))

        if body != b'x' * BIGFILE_SIZE:
            self.fail("Body != 'x' * %d. Got %r instead (%d bytes)." %
                      (BIGFILE_SIZE, body[:50], len(body)))
        conn.close()

    def test_file_stream_deadlock(self):
        if cherrypy.server.protocol_version != 'HTTP/1.1':
            return self.skip()

        self.PROTOCOL = 'HTTP/1.1'

        # Make an initial request but abort early.
        self.persistent = True
        conn = self.HTTP_CONN
        conn.putrequest('GET', '/bigfile', skip_host=True)
        conn.putheader('Host', self.HOST)
        conn.endheaders()
        response = conn.response_class(conn.sock, method='GET')
        response.begin()
        self.assertEqual(response.status, 200)
        body = response.fp.read(65536)
        if body != b'x' * len(body):
            self.fail("Body != 'x' * %d. Got %r instead (%d bytes)." %
                      (65536, body[:50], len(body)))
        response.close()
        conn.close()

        # Make a second request, which should fetch the whole file.
        self.persistent = False
        self.getPage('/bigfile')
        if self.body != b'x' * BIGFILE_SIZE:
            self.fail("Body != 'x' * %d. Got %r instead (%d bytes)." %
                      (BIGFILE_SIZE, self.body[:50], len(body)))

    def test_error_page_with_serve_file(self):
        self.getPage('/404test/yunyeen')
        self.assertStatus(404)
        self.assertInBody("I couldn't find that thing")

    def test_null_bytes(self):
        self.getPage('/static/\x00')
        self.assertStatus('404 Not Found')

    @staticmethod
    @contextlib.contextmanager
    def unicode_file():
        filename = ntou('?????????? ??????????????.html', 'utf-8')
        filepath = os.path.join(curdir, 'static', filename)
        with io.open(filepath, 'w', encoding='utf-8') as strm:
            strm.write(ntou('???????????? ??????????!', 'utf-8'))
        try:
            yield
        finally:
            os.remove(filepath)

    py27_on_windows = (
        platform.system() == 'Windows' and
        sys.version_info < (3,)
    )
    @pytest.mark.xfail(py27_on_windows, reason="#1544")
    def test_unicode(self):
        with self.unicode_file():
            url = ntou('/static/?????????? ??????????????.html', 'utf-8')
            # quote function requires str
            url = tonative(url, 'utf-8')
            url = urllib.parse.quote(url)
            self.getPage(url)

            expected = ntou('???????????? ??????????!', 'utf-8')
            self.assertInBody(expected)


def error_page_404(status, message, traceback, version):
    path = os.path.join(curdir, 'static', '404.html')
    return static.serve_file(path, content_type='text/html')
