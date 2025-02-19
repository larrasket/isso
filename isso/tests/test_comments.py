# -*- encoding: utf-8 -*-

import json
import os
import re
import tempfile
import unittest

from urllib.parse import urlencode

from werkzeug.wrappers import Response

from isso import Isso, core, config
from isso.utils import http
from isso.views import comments

from fixtures import curl, loads, FakeIP, FakeHost, JSONClient
http.curl = curl


class TestComments(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        conf = config.load(config.default_file())
        conf.set("general", "dbpath", self.path)
        conf.set("guard", "enabled", "off")
        conf.set("hash", "algorithm", "none")
        conf.set("general", "latest-enabled", "true")
        self.conf = conf

        class App(Isso, core.Mixin):
            pass

        self.app = App(conf)
        self.app.wsgi_app = FakeIP(self.app.wsgi_app, "192.168.1.1")

        self.client = JSONClient(self.app, Response)
        self.get = self.client.get
        self.put = self.client.put
        self.post = self.client.post
        self.delete = self.client.delete

    def tearDown(self):
        os.unlink(self.path)

    def testGet(self):

        self.post('/new?uri=%2Fpath%2F',
                  data=json.dumps({'text': 'Lorem ipsum ...'}))
        r = self.get('/id/1')
        self.assertEqual(r.status_code, 200)

        rv = loads(r.data)

        self.assertEqual(rv['id'], 1)
        self.assertEqual(rv['text'], '<p>Lorem ipsum ...</p>')

    def testCreate(self):

        rv = self.post('/new?uri=%2Fpath%2F',
                       data=json.dumps({'text': 'Lorem ipsum ...'}))

        self.assertEqual(rv.status_code, 201)
        self.assertIn("Set-Cookie", rv.headers)

        rv = loads(rv.data)

        self.assertEqual(rv["mode"], 1)
        self.assertEqual(rv["text"], '<p>Lorem ipsum ...</p>')

    def textCreateWithNonAsciiText(self):

        rv = self.post('/new?uri=%2Fpath%2F',
                       data=json.dumps({'text': 'Здравствуй, мир!'}))

        self.assertEqual(rv.status_code, 201)
        rv = loads(rv.data)

        self.assertEqual(rv["mode"], 1)
        self.assertEqual(rv["text"], '<p>Здравствуй, мир!</p>')

    def testCreateMultiple(self):

        a = self.post('/new?uri=test', data=json.dumps({'text': '...'}))
        b = self.post('/new?uri=test', data=json.dumps({'text': '...'}))
        c = self.post('/new?uri=test', data=json.dumps({'text': '...'}))

        self.assertEqual(loads(a.data)["id"], 1)
        self.assertEqual(loads(b.data)["id"], 2)
        self.assertEqual(loads(c.data)["id"], 3)

    def testCreateAndGetMultiple(self):

        for i in range(20):
            self.post('/new?uri=%2Fpath%2F', data=json.dumps({'text': 'Spam'}))

        r = self.get('/?uri=%2Fpath%2F')
        self.assertEqual(r.status_code, 200)

        rv = loads(r.data)
        self.assertEqual(len(rv['replies']), 20)

    def testCreateInvalidParent(self):

        self.post('/new?uri=test', data=json.dumps({'text': '...'}))
        self.post('/new?uri=test',
                  data=json.dumps({'text': '...', 'parent': 1}))
        invalid = self.post(
            '/new?uri=test', data=json.dumps({'text': '...', 'parent': 2}))

        self.assertEqual(loads(invalid.data)["parent"], 1)

    def testCreateInvalidThreadForParent(self):

        self.post('/new?uri=one', data=json.dumps({'text': '...'}))
        # Parent which is not in same thread should be rejected, set to None
        invalid = self.post(
            '/new?uri=two', data=json.dumps({'text': '...', 'parent': 1}))
        # Replies to commments in thread "two" are valid
        valid = self.post(
            '/new?uri=two', data=json.dumps({'text': '...', 'parent': 2}))

        self.assertEqual(loads(invalid.data)["parent"], None)
        self.assertEqual(loads(valid.data)["parent"], 2)

        # Insert (invalid) comment into thread "two" with parent from thread 1
        self.app.db.execute([
            'INSERT INTO COMMENTS (tid, parent, created, modified, mode,'
            '   remote_addr, text, author, email, website, voters, notification)',
            'SELECT threads.id, ?, ?, ?, ?, ?,     ?, ?, ?, ?, ?, ?',
            'FROM threads where threads.uri = ?;'],
            (None, 0.0, 0.0, 1, None, 'Text', None, None, None, bytes(1), None, 'two')
        )
        # For id=4, the parent has id=1, but is from thread "one". Because id=1
        # does not belong to the current thread "two", it is rejected and id=4
        # chosen instead.
        impossible = self.post(
            '/new?uri=two', data=json.dumps({'text': '...', 'parent': 4}))
        self.assertEqual(loads(impossible.data)["parent"], 4)

    def testVerifyFields(self):

        def verify(comment):
            return comments.API.verify(comment)[0]

        # text is missing
        self.assertFalse(verify({}))

        # invalid types
        self.assertFalse(verify({"text": "...", "parent": "xxx"}))
        for key in ("author", "website", "email"):
            self.assertFalse(verify({"text": True, key: 3.14}))

        # text too short and/or blank
        for text in ("", "\n\n\n"):
            self.assertFalse(verify({"text": text}))

        # email/website length
        self.assertTrue(verify({"text": "...", "email": "*" * 254}))
        self.assertTrue(
            verify({"text": "...", "website": "google.de/" + "a" * 128}))

        self.assertFalse(verify({"text": "...", "email": "*" * 1024}))
        self.assertFalse(
            verify({"text": "...", "website": "google.de/" + "*" * 1024}))

        # valid website url
        self.assertTrue(comments.isurl("example.tld"))
        self.assertTrue(comments.isurl("http://example.tld"))
        self.assertTrue(comments.isurl("https://example.tld"))
        self.assertTrue(comments.isurl("https://exämple.tld"))
        self.assertTrue(comments.isurl("https://example.tld:1337/"))
        self.assertTrue(comments.isurl("https://example.tld:1337/foobar"))
        self.assertTrue(comments.isurl(
            "https://example.tld:1337/foobar?p=1#isso-thread"))

        self.assertFalse(comments.isurl("ftp://example.tld/"))
        self.assertFalse(comments.isurl("tel:+1234567890"))
        self.assertFalse(comments.isurl("+1234567890"))
        self.assertFalse(comments.isurl("spam"))

    def testGetInvalid(self):

        self.assertEqual(self.get('/?uri=%2Fpath%2F&id=123').status_code, 200)
        data = loads(self.get('/?uri=%2Fpath%2F&id=123').data)
        self.assertEqual(len(data['replies']), 0)

        self.assertEqual(
            self.get('/?uri=%2Fpath%2Fspam%2F&id=123').status_code, 200)
        data = loads(self.get('/?uri=%2Fpath%2Fspam%2F&id=123').data)
        self.assertEqual(len(data['replies']), 0)

        self.assertEqual(self.get('/?uri=?uri=%foo%2F').status_code, 200)
        data = loads(self.get('/?uri=?uri=%foo%2F').data)
        self.assertEqual(len(data['replies']), 0)

    def testFetchEmpty(self):

        empty = self.get('/?uri=%2Fempty%2F')
        # Empty database returns 200, not 404
        self.assertEqual(empty.status_code, 200)
        data = loads(empty.data)
        self.assertEqual(data['total_replies'], 0)
        self.assertEqual(data['id'], None)

    def testGetLimited(self):

        for i in range(20):
            self.post('/new?uri=test', data=json.dumps({'text': '...'}))

        r = self.get('/?uri=test&limit=10')
        self.assertEqual(r.status_code, 200)

        rv = loads(r.data)
        self.assertEqual(len(rv['replies']), 10)

    def testGetNested(self):

        self.post('/new?uri=test', data=json.dumps({'text': '...'}))
        self.post('/new?uri=test',
                  data=json.dumps({'text': '...', 'parent': 1}))

        r = self.get('/?uri=test&parent=1')
        self.assertEqual(r.status_code, 200)

        rv = loads(r.data)
        self.assertEqual(len(rv['replies']), 1)

    def testGetLimitedNested(self):

        self.post('/new?uri=test', data=json.dumps({'text': '...'}))
        for i in range(20):
            self.post('/new?uri=test',
                      data=json.dumps({'text': '...', 'parent': 1}))

        r = self.get('/?uri=test&parent=1&limit=10')
        self.assertEqual(r.status_code, 200)

        rv = loads(r.data)
        self.assertEqual(len(rv['replies']), 10)

    def testUpdate(self):

        self.post('/new?uri=%2Fpath%2F',
                  data=json.dumps({'text': 'Lorem ipsum ...'}))
        self.put('/id/1', data=json.dumps({
            'text': 'Hello World', 'author': 'me', 'website': 'http://example.com/'}))

        r = self.get('/id/1?plain=1')
        self.assertEqual(r.status_code, 200)

        rv = loads(r.data)
        self.assertEqual(rv['text'], 'Hello World')
        self.assertEqual(rv['author'], 'me')
        self.assertEqual(rv['website'], 'http://example.com/')
        self.assertIn('modified', rv)

    def testDelete(self):

        self.post('/new?uri=%2Fpath%2F',
                  data=json.dumps({'text': 'Lorem ipsum ...'}))
        r = self.delete('/id/1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(loads(r.data), None)
        self.assertEqual(self.get('/id/1').status_code, 404)

    def testFetchAuthorization(self):
        self.post('/new?uri=%2Fpath%2F',
                  data=json.dumps({'text': 'Lorem ipsum ...'}))

        r = self.get('/id/1?plain=1')
        self.assertEqual(r.status_code, 200)

        self.client.delete_cookie('localhost.local', '1')
        r = self.get('/id/1?plain=1')
        self.assertEqual(r.status_code, 403)

    def testDeleteWithReference(self):

        client = JSONClient(self.app, Response)
        client.post('/new?uri=%2Fpath%2F', data=json.dumps({'text': 'First'}))
        client.post('/new?uri=%2Fpath%2F',
                    data=json.dumps({'text': 'First', 'parent': 1}))

        r = client.delete('/id/1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(loads(r.data)['mode'], 4)
        self.assertIn('/path/', self.app.db.threads)

        data = loads(client.get("/?uri=%2Fpath%2F").data)
        self.assertEqual(data["total_replies"], 1)

        self.assertEqual(self.get('/?uri=%2Fpath%2F&id=1').status_code, 200)
        self.assertEqual(self.get('/?uri=%2Fpath%2F&id=2').status_code, 200)

        r = client.delete('/id/2')
        self.assertEqual(self.get('/?uri=%2Fpath%2F').status_code, 200)
        self.assertNotIn('/path/', self.app.db.threads)

        data = loads(client.get('/?uri=%2Fpath%2F').data)
        self.assertEqual(len(data['replies']), 0)

    def testDeleteWithMultipleReferences(self):
        """
        [ comment 1 ]
            |
            --- [ comment 2, ref 1 ]
            |
            --- [ comment 3, ref 1 ]
        [ comment 4 ]
        """
        client = JSONClient(self.app, Response)

        client.post('/new?uri=%2Fpath%2F', data=json.dumps({'text': 'First'}))
        client.post('/new?uri=%2Fpath%2F',
                    data=json.dumps({'text': 'Second', 'parent': 1}))
        client.post('/new?uri=%2Fpath%2F',
                    data=json.dumps({'text': 'Third', 'parent': 1}))
        client.post('/new?uri=%2Fpath%2F', data=json.dumps({'text': 'Last'}))

        client.delete('/id/1')
        self.assertEqual(self.get('/?uri=%2Fpath%2F').status_code, 200)
        client.delete('/id/2')
        self.assertEqual(self.get('/?uri=%2Fpath%2F').status_code, 200)
        client.delete('/id/3')
        self.assertEqual(self.get('/?uri=%2Fpath%2F').status_code, 200)
        client.delete('/id/4')
        self.assertEqual(self.get('/?uri=%2Fpath%2F').status_code, 200)

        data = loads(client.get('/?uri=%2Fpath%2F').data)
        self.assertEqual(len(data['replies']), 0)

    def testPathVariations(self):

        paths = ['/sub/path/', '/path.html', '/sub/path.html', 'path', '/']

        for path in paths:
            self.assertEqual(self.post('/new?' + urlencode({'uri': path}),
                                       data=json.dumps({'text': '...'})).status_code, 201)

        for i, path in enumerate(paths):
            self.assertEqual(
                self.get('/?' + urlencode({'uri': path})).status_code, 200)
            self.assertEqual(self.get('/id/%i' % (i + 1)).status_code, 200)

    def testDeleteAndCreateByDifferentUsersButSamePostId(self):

        mallory = JSONClient(self.app, Response)
        mallory.post('/new?uri=%2Fpath%2F', data=json.dumps({'text': 'Foo'}))
        mallory.delete('/id/1')

        bob = JSONClient(self.app, Response)
        bob.post('/new?uri=%2Fpath%2F', data=json.dumps({'text': 'Bar'}))

        self.assertEqual(mallory.delete('/id/1').status_code, 403)
        self.assertEqual(bob.delete('/id/1').status_code, 200)

    def testHash(self):

        a = self.post('/new?uri=%2Fpath%2F', data=json.dumps({"text": "Aaa"}))
        b = self.post('/new?uri=%2Fpath%2F', data=json.dumps({"text": "Bbb"}))
        c = self.post('/new?uri=%2Fpath%2F',
                      data=json.dumps({"text": "Ccc", "email": "..."}))

        a = loads(a.data)
        b = loads(b.data)
        c = loads(c.data)

        self.assertNotEqual(a['hash'], '192.168.1.1')
        self.assertEqual(a['hash'], b['hash'])
        self.assertNotEqual(a['hash'], c['hash'])

    def testVisibleFields(self):

        rv = self.post('/new?uri=%2Fpath%2F',
                       data=json.dumps({"text": "...", "invalid": "field"}))
        self.assertEqual(rv.status_code, 201)

        rv = loads(rv.data)

        for key in comments.API.FIELDS:
            if key in rv:
                rv.pop(key)

        self.assertListEqual(list(rv.keys()), [])

    def testNoFeed(self):
        rv = self.get('/feed?uri=%2Fpath%2Fnothing')
        self.assertEqual(rv.status_code, 404)

    def testFeedEmpty(self):
        self.conf.set("rss", "base", "https://example.org")

        rv = self.get('/feed?uri=%2Fpath%2Fnothing')
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.headers['ETag'], '"empty"')
        data = rv.data.decode('utf-8')
        self.assertEqual(data, """<?xml version=\'1.0\' encoding=\'utf-8\'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:thr="http://purl.org/syndication/thread/1.0"><updated>1970-01-01T01:00:00Z</updated><id>tag:example.org,2018:/isso/thread/path/nothing</id><title>Comments for example.org/path/nothing</title></feed>""")

    def testFeed(self):
        self.conf.set("rss", "base", "https://example.org")

        self.post('/new?uri=%2Fpath%2F', data=json.dumps({'text': 'First'}))
        self.post('/new?uri=%2Fpath%2F',
                  data=json.dumps({'text': '*Second*', 'parent': 1}))

        rv = self.get('/feed?uri=%2Fpath%2F')
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.headers['ETag'], '"1-2"')
        data = rv.data.decode('utf-8')
        data = re.sub('[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]+Z',
                      '2018-04-01T10:00:00Z', data)
        self.maxDiff = None
        # Two accepted outputs, since different versions of Python sort attributes in different order.
        self.assertIn(data, ["""<?xml version=\'1.0\' encoding=\'utf-8\'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:thr="http://purl.org/syndication/thread/1.0"><updated>2018-04-01T10:00:00Z</updated><id>tag:example.org,2018:/isso/thread/path/</id><title>Comments for example.org/path/</title><entry><id>tag:example.org,2018:/isso/1/2</id><title>Comment #2</title><updated>2018-04-01T10:00:00Z</updated><author><name /></author><link href="https://example.org/path/#isso-2" /><content type="html">&lt;p&gt;&lt;em&gt;Second&lt;/em&gt;&lt;/p&gt;</content><thr:in-reply-to href="https://example.org/path/#isso-1" ref="tag:example.org,2018:/isso/1/1" /></entry><entry><id>tag:example.org,2018:/isso/1/1</id><title>Comment #1</title><updated>2018-04-01T10:00:00Z</updated><author><name /></author><link href="https://example.org/path/#isso-1" /><content type="html">&lt;p&gt;First&lt;/p&gt;</content></entry></feed>""", """<?xml version=\'1.0\' encoding=\'utf-8\'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:thr="http://purl.org/syndication/thread/1.0"><updated>2018-04-01T10:00:00Z</updated><id>tag:example.org,2018:/isso/thread/path/</id><title>Comments for example.org/path/</title><entry><id>tag:example.org,2018:/isso/1/2</id><title>Comment #2</title><updated>2018-04-01T10:00:00Z</updated><author><name /></author><link href="https://example.org/path/#isso-2" /><content type="html">&lt;p&gt;&lt;em&gt;Second&lt;/em&gt;&lt;/p&gt;</content><thr:in-reply-to ref="tag:example.org,2018:/isso/1/1" href="https://example.org/path/#isso-1" /></entry><entry><id>tag:example.org,2018:/isso/1/1</id><title>Comment #1</title><updated>2018-04-01T10:00:00Z</updated><author><name /></author><link href="https://example.org/path/#isso-1" /><content type="html">&lt;p&gt;First&lt;/p&gt;</content></entry></feed>"""])

    def testCounts(self):

        rv = self.post('/count', data=json.dumps(['/path/']))
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(loads(rv.data), [0])

        self.post('/new?uri=%2Fpath%2F', data=json.dumps({"text": "..."}))

        rv = self.post('/count', data=json.dumps(['/path/']))
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(loads(rv.data), [1])

        for x in range(3):
            self.post('/new?uri=%2Fpath%2F', data=json.dumps({"text": "..."}))

        rv = self.post('/count', data=json.dumps(['/path/']))
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(loads(rv.data), [4])

        for x in range(4):
            self.delete('/id/%i' % (x + 1))

        rv = self.post('/count', data=json.dumps(['/path/']))
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(loads(rv.data), [0])

    def testMultipleCounts(self):

        expected = {'a': 1, 'b': 2, 'c': 0}

        for uri, count in expected.items():
            for _ in range(count):
                self.post('/new?uri=%s' %
                          uri, data=json.dumps({"text": "..."}))

        rv = self.post('/count', data=json.dumps(list(expected.keys())))
        self.assertEqual(loads(rv.data), list(expected.values()))

    def testModify(self):
        self.post('/new?uri=test', data=json.dumps({"text": "Tpyo"}))

        self.put('/id/1', data=json.dumps({"text": "Tyop"}))
        self.assertEqual(loads(self.get('/id/1').data)["text"], "<p>Tyop</p>")

        self.put('/id/1', data=json.dumps({"text": "Typo"}))
        self.assertEqual(loads(self.get('/id/1').data)["text"], "<p>Typo</p>")

    def testDeleteCommentRemovesThread(self):

        self.client.post('/new?uri=%2F', data=json.dumps({"text": "..."}))
        self.assertIn('/', self.app.db.threads)
        self.client.delete('/id/1')
        self.assertNotIn('/', self.app.db.threads)

    def testCSRF(self):

        js = "application/json"
        form = "application/x-www-form-urlencoded"

        self.post('/new?uri=%2F', data=json.dumps({"text": "..."}))

        # no header is fine (default for XHR)
        self.assertEqual(
            self.post('/id/1/dislike', content_type="").status_code, 200)

        # x-www-form-urlencoded is definitely not RESTful
        self.assertEqual(
            self.post('/id/1/dislike', content_type=form).status_code, 403)
        self.assertEqual(self.post('/new?uri=%2F', data=json.dumps({"text": "..."}),
                                   content_type=form).status_code, 403)
        # just for the record
        self.assertEqual(
            self.post('/id/1/dislike', content_type=js).status_code, 200)

    def testPreview(self):
        response = self.post(
            '/preview', data=json.dumps({'text': 'This is **mark***down*'}))
        self.assertEqual(response.status_code, 200)

        rv = loads(response.data)
        self.assertEqual(
            rv["text"], '<p>This is <strong>mark</strong><em>down</em></p>')

    def testTitleNull(self):
        # Thread title set to `null` in API request
        # Javascript `null` equals Python `None`
        self.post('/new?uri=%2Fpath%2F',
                  data=json.dumps({'text': 'Spam', 'title': None}))

        thread = self.app.db.threads.get(1)
        # Expect server to attempt to parse uri to extract title
        # utils.parse cannot parse fake /path/, so default="Untitled."
        self.assertEqual(thread.get('title'), "Untitled.")

    def testLatestOk(self):
        # load some comments in a mix of posts
        saved = []
        for idx, post_id in enumerate([1, 2, 2, 1, 2, 1, 3, 1, 4, 2, 3, 4, 1, 2]):
            text = 'text-{}'.format(idx)
            post_uri = 'test-{}'.format(post_id)
            self.post('/new?uri=' + post_uri, data=json.dumps({'text': text}))
            saved.append((post_uri, text))

        response = self.get('/latest?limit=5')
        self.assertEqual(response.status_code, 200)

        body = loads(response.data)
        expected_items = saved[-5:]  # latest 5
        for reply, expected in zip(body, expected_items):
            expected_uri, expected_text = expected
            self.assertIn(expected_text, reply['text'])
            self.assertEqual(expected_uri, reply['uri'])

    def testLatestWithoutLimit(self):
        response = self.get('/latest')
        self.assertEqual(response.status_code, 400)

    def testLatestBadLimitNaN(self):
        response = self.get('/latest?limit=WAT')
        self.assertEqual(response.status_code, 400)

    def testLatestBadLimitNegative(self):
        response = self.get('/latest?limit=-12')
        self.assertEqual(response.status_code, 400)

    def testLatestBadLimitZero(self):
        response = self.get('/latest?limit=0')
        self.assertEqual(response.status_code, 400)

    def testLatestNotEnabled(self):
        # disable the endpoint
        self.conf.set("general", "latest-enabled", "false")

        response = self.get('/latest?limit=5')
        self.assertEqual(response.status_code, 404)


class TestHostDependent(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        conf = config.load(config.default_file())
        conf.set("general", "dbpath", self.path)
        self.conf = conf

        class App(Isso, core.Mixin):
            pass

        self.app = App(conf)

        self.client = JSONClient(self.app, Response)
        self.post = self.client.post

    def tearDown(self):
        os.unlink(self.path)

    def testSecureCookieNoConf(self):
        self.app.wsgi_app = FakeHost(self.app.wsgi_app, "isso-dev.local", "https")
        rv = self.post('/new?uri=%2Fpath%2F',
                       data=json.dumps({'text': 'Lorem ipsum ...'}))

        self.assertIn("Secure", rv.headers["Set-Cookie"])
        self.assertIn("Secure", rv.headers["X-Set-Cookie"])
        self.assertIn("SameSite=None", rv.headers["Set-Cookie"])

    def testInSecureCookieNoConf(self):
        self.app.wsgi_app = FakeHost(self.app.wsgi_app, "isso-dev.local", "http")
        rv = self.post('/new?uri=%2Fpath%2F',
                       data=json.dumps({'text': 'Lorem ipsum ...'}))

        self.assertNotIn("Secure", rv.headers["Set-Cookie"])
        self.assertNotIn("Secure", rv.headers["X-Set-Cookie"])
        self.assertIn("SameSite=Lax", rv.headers["Set-Cookie"])

    def testSameSiteConfNone(self):
        # By default, isso should set SameSite=Lax when served over http
        self.app.wsgi_app = FakeHost(self.app.wsgi_app, "isso-dev.local", "http")
        # Conf overrides SameSite setting
        self.conf.set("server", "samesite", "None")

        rv = self.post('/new?uri=%2Fpath%2F',
                       data=json.dumps({'text': 'Lorem ipsum ...'}))

        self.assertNotIn("Secure", rv.headers["Set-Cookie"])
        self.assertNotIn("Secure", rv.headers["X-Set-Cookie"])
        self.assertIn("SameSite=None", rv.headers["Set-Cookie"])

    def testSameSiteConfLax(self):
        # By default, isso should set SameSite=None when served over https
        self.app.wsgi_app = FakeHost(self.app.wsgi_app, "isso-dev.local", "https")
        # Conf overrides SameSite setting
        self.conf.set("server", "samesite", "Lax")

        rv = self.post('/new?uri=%2Fpath%2F',
                       data=json.dumps({'text': 'Lorem ipsum ...'}))

        self.assertIn("Secure", rv.headers["Set-Cookie"])
        self.assertIn("Secure", rv.headers["X-Set-Cookie"])
        self.assertIn("SameSite=Lax", rv.headers["Set-Cookie"])


class TestModeratedComments(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        conf = config.load(config.default_file())
        conf.set("general", "dbpath", self.path)
        conf.set("moderation", "enabled", "true")
        conf.set("guard", "enabled", "off")
        conf.set("hash", "algorithm", "none")

        class App(Isso, core.Mixin):
            pass

        self.app = App(conf)
        self.app.wsgi_app = FakeIP(self.app.wsgi_app, "192.168.1.1")
        self.client = JSONClient(self.app, Response)

    def tearDown(self):
        os.unlink(self.path)

    def testAddComment(self):

        rv = self.client.post(
            '/new?uri=test', data=json.dumps({"text": "..."}))
        self.assertEqual(rv.status_code, 202)

        self.assertEqual(self.client.get('/id/1').status_code, 200)
        self.assertEqual(self.client.get('/?uri=test').status_code, 200)

        data = loads(self.client.get('/?uri=test').data)
        self.assertEqual(len(data['replies']), 0)

        self.app.db.comments.activate(1)
        self.assertEqual(self.client.get('/?uri=test').status_code, 200)

    def testModerateComment(self):

        id_ = 1
        signed = self.app.sign(id_)

        # Create new comment, should have mode=2 (pending moderation)
        rv = self.client.post(
            '/new?uri=/moderated', data=json.dumps({"text": "..."}))
        self.assertEqual(rv.status_code, 202)
        self.assertEqual(self.client.get('/id/1').status_code, 200)
        self.assertEqual(self.app.db.comments.get(id_)["mode"], 2)
        self.assertEqual(self.app.db.comments.get(id_)["text"], "...")

        # GET should return some html form
        action = "activate"
        rv_activate_get = self.client.get('/id/%d/%s/%s' % (id_, action, signed))
        self.assertEqual(rv_activate_get.status_code, 200)
        self.assertIn(b"Activate: Are you sure?", rv_activate_get.data)
        self.assertIn(b"http://invalid.local/moderated#isso-1", rv_activate_get.data)

        # Activate comment
        action = "activate"
        rv_activated = self.client.post('/id/%d/%s/%s' % (id_, action, signed))
        self.assertEqual(rv_activated.status_code, 200)
        self.assertEqual(rv_activated.data, b"Comment has been activated")

        # Activating should be idempotent
        rv_activated = self.client.post('/id/%d/%s/%s' % (id_, action, signed))
        self.assertEqual(rv_activated.status_code, 200)
        self.assertEqual(rv_activated.data, b"Already activated")

        # Comment should have mode=1 (activated)
        self.assertEqual(self.app.db.comments.get(id_)["mode"], 1)

        # Edit comment
        action = "edit"
        rv_edit = self.client.post('/id/%d/%s/%s' % (id_, action, signed), data=json.dumps({"text": "new text"}))
        self.assertEqual(rv_edit.status_code, 200)
        self.assertEqual(json.loads(rv_edit.data)["id"], id_)
        self.assertEqual(self.app.db.comments.get(id_)["text"], "new text")

        # Wrong action on comment is handled by the routing
        action = "foo"
        rv_wrong_action = self.client.post('/id/%d/%s/%s' % (id_, action, signed))
        self.assertEqual(rv_wrong_action.status_code, 404)

        # Delete comment
        action = "delete"
        rv_deleted = self.client.post('/id/%d/%s/%s' % (id_, action, signed))
        self.assertEqual(rv_deleted.status_code, 200)
        self.assertEqual(rv_deleted.data, b"Comment has been deleted")

        # Comment should no longer exist
        self.assertEqual(self.app.db.comments.get(id_), None)


class TestUnsubscribe(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        conf = config.load(config.default_file())
        conf.set("general", "dbpath", self.path)
        conf.set("moderation", "enabled", "true")
        conf.set("guard", "enabled", "off")
        conf.set("hash", "algorithm", "none")

        class App(Isso, core.Mixin):
            pass

        self.app = App(conf)
        self.app.wsgi_app = FakeIP(self.app.wsgi_app, "192.168.1.1")
        self.client = JSONClient(self.app, Response)

        # add default comment
        rv = self.client.post(
            '/new?uri=test', data=json.dumps({"text": "..."}))
        self.assertEqual(rv.status_code, 202)

    def tearDown(self):
        os.unlink(self.path)

    def testUnsubscribe(self):
        id_ = 1
        email = "test@test.example"
        key = self.app.sign(('unsubscribe', email))

        # GET should return some html form
        rv_unsubscribe_get = self.client.get('/id/%d/unsubscribe/%s/%s' % (id_, email, key))
        self.assertEqual(rv_unsubscribe_get.status_code, 200)
        self.assertIn(b"Successfully unsubscribed", rv_unsubscribe_get.data)

        # Incomplete key should fail
        key = self.app.sign(['unsubscribe'])
        rv_incomplete_key = self.client.get('/id/%d/unsubscribe/%s/%s' % (id_, email, key))
        self.assertEqual(rv_incomplete_key.status_code, 403)

        # Wrong key type should fail
        key = self.app.sign(1)
        rv_wrong_key_type = self.client.get('/id/%d/unsubscribe/%s/%s' % (id_, email, key))
        self.assertEqual(rv_wrong_key_type.status_code, 403)


class TestPurgeComments(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        conf = config.load(config.default_file())
        conf.set("general", "dbpath", self.path)
        conf.set("moderation", "enabled", "true")
        conf.set("guard", "enabled", "off")
        conf.set("hash", "algorithm", "none")

        class App(Isso, core.Mixin):
            pass

        self.app = App(conf)
        self.app.wsgi_app = FakeIP(self.app.wsgi_app, "192.168.1.1")
        self.client = JSONClient(self.app, Response)

    def testPurgeDoesNoHarm(self):
        self.client.post('/new?uri=test', data=json.dumps({"text": "..."}))
        self.app.db.comments.activate(1)
        self.app.db.comments.purge(0)
        self.assertEqual(self.client.get('/?uri=test').status_code, 200)

    def testPurgeWorks(self):
        self.client.post('/new?uri=test', data=json.dumps({"text": "..."}))
        self.app.db.comments.purge(0)
        self.assertEqual(self.client.get('/id/1').status_code, 404)

        self.client.post('/new?uri=test', data=json.dumps({"text": "..."}))
        self.app.db.comments.purge(3600)
        self.assertEqual(self.client.get('/id/1').status_code, 200)
