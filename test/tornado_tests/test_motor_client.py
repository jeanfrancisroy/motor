# Copyright 2012-2015 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import unicode_literals

"""Test Motor, an asynchronous driver for MongoDB and Tornado."""

import os
import unittest
import warnings

import bson
import pymongo
import pymongo.mongo_client
from bson import CodecOptions
from bson.binary import JAVA_LEGACY, UUID_SUBTYPE
from mockupdb import OpQuery
from pymongo import ReadPreference, WriteConcern
from pymongo.errors import ConfigurationError, OperationFailure
from pymongo.errors import ConnectionFailure
from tornado import gen, version_info as tornado_version
from tornado.concurrent import Future
from tornado.ioloop import IOLoop
from tornado.testing import gen_test

import motor
import test
from test import SkipTest
from test.test_environment import db_user, db_password, env
from test.tornado_tests import remove_all_users, MotorTest, MotorMockServerTest
from test.utils import one, ignore_deprecations


class MotorClientTest(MotorTest):
    def test_host_port_deprecated(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with self.assertRaises(DeprecationWarning):
                self.cx.host

            with self.assertRaises(DeprecationWarning):
                self.cx.port

    def test_document_class_deprecated(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with self.assertRaises(DeprecationWarning):
                self.cx.document_class

            with self.assertRaises(DeprecationWarning):
                # Setting the property is deprecated, too.
                self.cx.document_class = bson.SON

    @gen_test
    def test_client_open(self):
        cx = self.motor_client()
        with ignore_deprecations():
            self.assertEqual(cx, (yield cx.open()))
            self.assertEqual(cx, (yield cx.open()))  # Same the second time.

    @gen_test
    def test_client_lazy_connect(self):
        yield self.db.test_client_lazy_connect.remove()

        # Create client without connecting; connect on demand.
        cx = self.motor_client()
        collection = cx.motor_test.test_client_lazy_connect
        future0 = collection.insert({'foo': 'bar'})
        future1 = collection.insert({'foo': 'bar'})
        yield [future0, future1]

        self.assertEqual(2, (yield collection.find({'foo': 'bar'}).count()))

        cx.close()

    @gen_test
    def test_close(self):
        cx = self.motor_client()
        cx.close()
        self.assertEqual(None, cx._get_primary_pool())

    @gen_test
    def test_unix_socket(self):
        mongodb_socket = '/tmp/mongodb-%d.sock' % env.port
        if not os.access(mongodb_socket, os.R_OK):
            raise SkipTest("Socket file is not accessible")

        uri = 'mongodb://%s' % mongodb_socket
        client = self.motor_client(uri)

        if test.env.auth:
            yield client.admin.authenticate(db_user, db_password)

        yield client.motor_test.test.save({"dummy": "object"})

        # Confirm it fails with a missing socket.
        client = motor.MotorClient(
            "mongodb:///tmp/non-existent.sock", io_loop=self.io_loop)

        with self.assertRaises(ConnectionFailure):
            yield client.admin.command('ping')

    def test_io_loop(self):
        with self.assertRaises(TypeError):
            motor.MotorClient(test.env.uri, io_loop='foo')

    def test_open_sync(self):
        loop = IOLoop()

        with ignore_deprecations():
            cx = loop.run_sync(self.motor_client(io_loop=loop).open)

        self.assertTrue(isinstance(cx, motor.MotorClient))

    def test_database_named_delegate(self):
        self.assertTrue(
            isinstance(self.cx.delegate, pymongo.mongo_client.MongoClient))
        self.assertTrue(isinstance(self.cx['delegate'],
                                   motor.MotorDatabase))

    @gen_test
    def test_connection_failure(self):
        # Assuming there isn't anything actually running on this port
        client = motor.MotorClient('localhost', 8765, io_loop=self.io_loop)

        # Test the Future interface.
        with self.assertRaises(ConnectionFailure):
            yield client.admin.command('ping')

        # Test with a callback.
        with ignore_deprecations():
            (result, error), _ = yield gen.Task(client.open)

        self.assertEqual(None, result)
        self.assertTrue(isinstance(error, ConnectionFailure))

    @gen_test(timeout=30)
    def test_connection_timeout(self):
        # Motor merely tries to time out a connection attempt within the
        # specified duration; DNS lookup in particular isn't charged against
        # the timeout. So don't measure how long this takes.
        client = motor.MotorClient(
            'example.com', port=12345,
            connectTimeoutMS=1, io_loop=self.io_loop)

        with self.assertRaises(ConnectionFailure):
            yield client.admin.command('ping')

    @gen_test
    def test_max_pool_size_validation(self):
        with self.assertRaises(ConfigurationError):
            motor.MotorClient(max_pool_size=-1)

        with self.assertRaises(ConfigurationError):
            motor.MotorClient(max_pool_size='foo')

        cx = self.motor_client(max_pool_size=100)
        self.assertEqual(cx.max_pool_size, 100)
        cx.close()

    @gen_test(timeout=60)
    def test_high_concurrency(self):
        if tornado_version < (4, 0, 0, 0):
            raise SkipTest("MOTOR-73")

        yield self.make_test_data()

        concurrency = 25
        cx = self.motor_client(max_pool_size=concurrency)
        expected_finds = 200 * concurrency
        n_inserts = 25

        collection = cx.motor_test.test_collection
        insert_collection = cx.motor_test.insert_collection
        yield insert_collection.remove()

        ndocs = [0]
        insert_future = Future()

        @gen.coroutine
        def find():
            cursor = collection.find()
            while (yield cursor.fetch_next):
                cursor.next_object()
                ndocs[0] += 1

                # Half-way through, start an insert loop
                if ndocs[0] == expected_finds / 2:
                    insert()

        @gen.coroutine
        def insert():
            for i in range(n_inserts):
                yield insert_collection.insert({'s': hex(i)})

            insert_future.set_result(None)  # Finished

        yield [find() for _ in range(concurrency)]
        yield insert_future
        self.assertEqual(expected_finds, ndocs[0])
        self.assertEqual(n_inserts, (yield insert_collection.count()))
        yield collection.remove()

    @gen_test(timeout=30)
    def test_drop_database(self):
        # Make sure we can pass a MotorDatabase instance to drop_database
        db = self.cx.test_drop_database
        yield db.test_collection.insert({})
        names = yield self.cx.database_names()
        self.assertTrue('test_drop_database' in names)
        yield self.cx.drop_database(db)
        names = yield self.cx.database_names()
        self.assertFalse('test_drop_database' in names)

    @gen_test
    def test_auth_from_uri(self):
        if not test.env.auth:
            raise SkipTest('Authentication is not enabled on server')

        # self.db is logged in as root.
        yield remove_all_users(self.db)
        db = self.db
        try:
            yield db.add_user(
                'mike', 'password',
                roles=['userAdmin', 'readWrite'])

            client = motor.MotorClient(
                'mongodb://u:pass@%s:%d' % (env.host, env.port),
                io_loop=self.io_loop)

            # ismaster doesn't throw auth errors.
            yield client.admin.command('ismaster')

            with self.assertRaises(OperationFailure):
                yield client.db.collection.find_one()

            client = motor.MotorClient(
                'mongodb://mike:password@%s:%d/%s' %
                (env.host, env.port, db.name),
                io_loop=self.io_loop)

            yield client[db.name].collection.find_one()
        finally:
            yield db.remove_user('mike')

    @gen_test
    def test_socketKeepAlive(self):
        # Connect.
        yield self.cx.server_info()
        ka = self.cx._get_primary_pool().socket_keepalive
        self.assertFalse(ka)

        client = self.motor_client(socketKeepAlive=True)
        yield client.server_info()
        ka = client._get_primary_pool().socket_keepalive
        self.assertTrue(ka)

    def test_uuid_subtype(self):
        cx = self.motor_client(uuidRepresentation='javaLegacy')

        with ignore_deprecations():
            self.assertEqual(cx.uuid_subtype, JAVA_LEGACY)
            cx.uuid_subtype = UUID_SUBTYPE
            self.assertEqual(cx.uuid_subtype, UUID_SUBTYPE)
            self.assertEqual(cx.delegate.uuid_subtype, UUID_SUBTYPE)

    def test_get_database(self):
        codec_options = CodecOptions(tz_aware=True)
        write_concern = WriteConcern(w=2, j=True)
        db = self.cx.get_database(
            'foo', codec_options, ReadPreference.SECONDARY, write_concern)

        self.assertTrue(isinstance(db, motor.MotorDatabase))
        self.assertEqual('foo', db.name)
        self.assertEqual(codec_options, db.codec_options)
        self.assertEqual(ReadPreference.SECONDARY, db.read_preference)
        self.assertEqual(write_concern.document, db.write_concern)


class MotorClientTimeoutTest(MotorMockServerTest):
    @gen_test
    def test_timeout(self):
        if tornado_version < (4, 0, 0, 0):
            raise SkipTest("MOTOR-73")

        server = self.server(auto_ismaster=True)
        client = motor.MotorClient(server.uri, socketTimeoutMS=100)

        with self.assertRaises(pymongo.errors.AutoReconnect) as context:
            yield client.motor_test.test_collection.find_one()

        self.assertEqual(str(context.exception), 'timed out')
        client.close()


class MotorClientExhaustCursorTest(MotorMockServerTest):
    def primary_server(self):
        primary = self.server()
        hosts = [primary.address_string]
        primary.autoresponds(
            'ismaster', ismaster=True, setName='rs', hosts=hosts)

        return primary

    def primary_or_standalone(self, rs):
        if rs:
            return self.primary_server()
        else:
            return self.server(auto_ismaster=True)

    @gen.coroutine
    def _test_exhaust_query_server_error(self, rs):
        # When doing an exhaust query, the socket stays checked out on success
        # but must be checked in on error to avoid counter leak.
        server = self.primary_or_standalone(rs=rs)
        client = motor.MotorClient(server.uri, max_pool_size=1)
        yield client.admin.command('ismaster')
        pool = client._get_primary_pool()
        sock_info = one(pool.sockets)
        cursor = client.db.collection.find(exhaust=True)

        # With Tornado, simply accessing fetch_next starts the fetch.
        fetch_next = cursor.fetch_next
        request = yield self.run_thread(server.receives, OpQuery)
        request.fail()

        with self.assertRaises(pymongo.errors.OperationFailure):
            yield fetch_next

        self.assertFalse(sock_info.closed)
        self.assertEqual(sock_info, one(pool.sockets))

    @gen_test
    def test_exhaust_query_server_error_standalone(self):
        yield self._test_exhaust_query_server_error(rs=False)

    @gen_test
    def test_exhaust_query_server_error_rs(self):
        yield self._test_exhaust_query_server_error(rs=True)

    @gen.coroutine
    def _test_exhaust_query_network_error(self, rs):
        # When doing an exhaust query, the socket stays checked out on success
        # but must be checked in on error to avoid counter leak.
        server = self.primary_or_standalone(rs=rs)
        client = motor.MotorClient(server.uri, max_pool_size=1)

        yield client.admin.command('ismaster')
        pool = client._get_primary_pool()
        pool._check_interval_seconds = None  # Never check.
        sock_info = one(pool.sockets)

        cursor = client.db.collection.find(exhaust=True)

        # With Tornado, simply accessing fetch_next starts the fetch.
        fetch_next = cursor.fetch_next
        request = yield self.run_thread(server.receives, OpQuery)
        request.hangs_up()

        with self.assertRaises(pymongo.errors.ConnectionFailure):
            yield fetch_next

        self.assertTrue(sock_info.closed)
        del cursor
        self.assertNotIn(sock_info, pool.sockets)

    @gen_test
    def test_exhaust_query_network_error_standalone(self):
        yield self._test_exhaust_query_network_error(rs=False)

    @gen_test
    def test_exhaust_query_network_error_rs(self):
        yield self._test_exhaust_query_network_error(rs=True)


if __name__ == '__main__':
    unittest.main()
