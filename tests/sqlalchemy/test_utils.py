# Copyright (c) 2013 Boris Pavlovic (boris@pavlovic.me).
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import fixtures
import uuid
import warnings

from migrate.changeset import UniqueConstraint
import mock
from oslotest import base as test_base
from oslotest import moxstubout
import six
from six import moves
from six.moves.urllib import parse
import sqlalchemy
from sqlalchemy.dialects import mysql
from sqlalchemy import Boolean, Index, Integer, DateTime, String
from sqlalchemy import MetaData, Table, Column, ForeignKey
from sqlalchemy.engine import reflection
from sqlalchemy.exc import SAWarning, ResourceClosedError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import select
from sqlalchemy.types import UserDefinedType, NullType

from oslo.db import exception
from oslo.db.sqlalchemy import models
from oslo.db.sqlalchemy import session
from oslo.db.sqlalchemy import test_base as db_test_base
from oslo.db.sqlalchemy import utils
from tests import utils as test_utils


SA_VERSION = tuple(map(int, sqlalchemy.__version__.split('.')))


class TestSanitizeDbUrl(test_base.BaseTestCase):

    def test_url_with_cred(self):
        db_url = 'myproto://johndoe:secret@localhost/myschema'
        expected = 'myproto://****:****@localhost/myschema'
        actual = utils.sanitize_db_url(db_url)
        self.assertEqual(expected, actual)

    def test_url_with_no_cred(self):
        db_url = 'sqlite:///mysqlitefile'
        actual = utils.sanitize_db_url(db_url)
        self.assertEqual(db_url, actual)


class CustomType(UserDefinedType):
    """Dummy column type for testing unsupported types."""
    def get_col_spec(self):
        return "CustomType"


class FakeModel(object):
    def __init__(self, values):
        self.values = values

    def __getattr__(self, name):
        try:
            value = self.values[name]
        except KeyError:
            raise AttributeError(name)
        return value

    def __getitem__(self, key):
        if key in self.values:
            return self.values[key]
        else:
            raise NotImplementedError()

    def __repr__(self):
        return '<FakeModel: %s>' % self.values


class TestPaginateQuery(test_base.BaseTestCase):
    def setUp(self):
        super(TestPaginateQuery, self).setUp()
        mox_fixture = self.useFixture(moxstubout.MoxStubout())
        self.mox = mox_fixture.mox
        self.query = self.mox.CreateMockAnything()
        self.mox.StubOutWithMock(sqlalchemy, 'asc')
        self.mox.StubOutWithMock(sqlalchemy, 'desc')
        self.marker = FakeModel({
            'user_id': 'user',
            'project_id': 'p',
            'snapshot_id': 's',
        })
        self.model = FakeModel({
            'user_id': 'user',
            'project_id': 'project',
            'snapshot_id': 'snapshot',
        })

    def test_paginate_query_no_pagination_no_sort_dirs(self):
        sqlalchemy.asc('user').AndReturn('asc_3')
        self.query.order_by('asc_3').AndReturn(self.query)
        sqlalchemy.asc('project').AndReturn('asc_2')
        self.query.order_by('asc_2').AndReturn(self.query)
        sqlalchemy.asc('snapshot').AndReturn('asc_1')
        self.query.order_by('asc_1').AndReturn(self.query)
        self.query.limit(5).AndReturn(self.query)
        self.mox.ReplayAll()
        utils.paginate_query(self.query, self.model, 5,
                             ['user_id', 'project_id', 'snapshot_id'])

    def test_paginate_query_no_pagination(self):
        sqlalchemy.asc('user').AndReturn('asc')
        self.query.order_by('asc').AndReturn(self.query)
        sqlalchemy.desc('project').AndReturn('desc')
        self.query.order_by('desc').AndReturn(self.query)
        self.query.limit(5).AndReturn(self.query)
        self.mox.ReplayAll()
        utils.paginate_query(self.query, self.model, 5,
                             ['user_id', 'project_id'],
                             sort_dirs=['asc', 'desc'])

    def test_paginate_query_attribute_error(self):
        sqlalchemy.asc('user').AndReturn('asc')
        self.query.order_by('asc').AndReturn(self.query)
        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSortKey,
                          utils.paginate_query, self.query,
                          self.model, 5, ['user_id', 'non-existent key'])

    def test_paginate_query_assertion_error(self):
        self.mox.ReplayAll()
        self.assertRaises(AssertionError,
                          utils.paginate_query, self.query,
                          self.model, 5, ['user_id'],
                          marker=self.marker,
                          sort_dir='asc', sort_dirs=['asc'])

    def test_paginate_query_assertion_error_2(self):
        self.mox.ReplayAll()
        self.assertRaises(AssertionError,
                          utils.paginate_query, self.query,
                          self.model, 5, ['user_id'],
                          marker=self.marker,
                          sort_dir=None, sort_dirs=['asc', 'desk'])

    def test_paginate_query(self):
        sqlalchemy.asc('user').AndReturn('asc_1')
        self.query.order_by('asc_1').AndReturn(self.query)
        sqlalchemy.desc('project').AndReturn('desc_1')
        self.query.order_by('desc_1').AndReturn(self.query)
        self.mox.StubOutWithMock(sqlalchemy.sql, 'and_')
        sqlalchemy.sql.and_(False).AndReturn('some_crit')
        sqlalchemy.sql.and_(True, False).AndReturn('another_crit')
        self.mox.StubOutWithMock(sqlalchemy.sql, 'or_')
        sqlalchemy.sql.or_('some_crit', 'another_crit').AndReturn('some_f')
        self.query.filter('some_f').AndReturn(self.query)
        self.query.limit(5).AndReturn(self.query)
        self.mox.ReplayAll()
        utils.paginate_query(self.query, self.model, 5,
                             ['user_id', 'project_id'],
                             marker=self.marker,
                             sort_dirs=['asc', 'desc'])

    def test_paginate_query_value_error(self):
        sqlalchemy.asc('user').AndReturn('asc_1')
        self.query.order_by('asc_1').AndReturn(self.query)
        self.mox.ReplayAll()
        self.assertRaises(ValueError, utils.paginate_query,
                          self.query, self.model, 5, ['user_id', 'project_id'],
                          marker=self.marker, sort_dirs=['asc', 'mixed'])


class TestMigrationUtils(db_test_base.DbTestCase):

    """Class for testing utils that are used in db migrations."""

    def setUp(self):
        super(TestMigrationUtils, self).setUp()
        self.meta = MetaData(bind=self.engine)
        self.conn = self.engine.connect()
        self.addCleanup(self.meta.drop_all)
        self.addCleanup(self.conn.close)

    def _populate_db_for_drop_duplicate_entries(self, engine, meta,
                                                table_name):
        values = [
            {'id': 11, 'a': 3, 'b': 10, 'c': 'abcdef'},
            {'id': 12, 'a': 5, 'b': 10, 'c': 'abcdef'},
            {'id': 13, 'a': 6, 'b': 10, 'c': 'abcdef'},
            {'id': 14, 'a': 7, 'b': 10, 'c': 'abcdef'},
            {'id': 21, 'a': 1, 'b': 20, 'c': 'aa'},
            {'id': 31, 'a': 1, 'b': 20, 'c': 'bb'},
            {'id': 41, 'a': 1, 'b': 30, 'c': 'aef'},
            {'id': 42, 'a': 2, 'b': 30, 'c': 'aef'},
            {'id': 43, 'a': 3, 'b': 30, 'c': 'aef'}
        ]

        test_table = Table(table_name, meta,
                           Column('id', Integer, primary_key=True,
                                  nullable=False),
                           Column('a', Integer),
                           Column('b', Integer),
                           Column('c', String(255)),
                           Column('deleted', Integer, default=0),
                           Column('deleted_at', DateTime),
                           Column('updated_at', DateTime))

        test_table.create()
        engine.execute(test_table.insert(), values)
        return test_table, values

    def test_drop_old_duplicate_entries_from_table(self):
        table_name = "__test_tmp_table__"

        test_table, values = self._populate_db_for_drop_duplicate_entries(
            self.engine, self.meta, table_name)
        utils.drop_old_duplicate_entries_from_table(
            self.engine, table_name, False, 'b', 'c')

        uniq_values = set()
        expected_ids = []
        for value in sorted(values, key=lambda x: x['id'], reverse=True):
            uniq_value = (('b', value['b']), ('c', value['c']))
            if uniq_value in uniq_values:
                continue
            uniq_values.add(uniq_value)
            expected_ids.append(value['id'])

        real_ids = [row[0] for row in
                    self.engine.execute(select([test_table.c.id])).fetchall()]

        self.assertEqual(len(real_ids), len(expected_ids))
        for id_ in expected_ids:
            self.assertTrue(id_ in real_ids)

    def test_drop_dup_entries_in_file_conn(self):
        table_name = "__test_tmp_table__"
        tmp_db_file = self.create_tempfiles([['name', '']], ext='.sql')[0]
        in_file_engine = session.EngineFacade(
            'sqlite:///%s' % tmp_db_file).get_engine()
        meta = MetaData()
        meta.bind = in_file_engine
        test_table, values = self._populate_db_for_drop_duplicate_entries(
            in_file_engine, meta, table_name)
        utils.drop_old_duplicate_entries_from_table(
            in_file_engine, table_name, False, 'b', 'c')

    def test_drop_old_duplicate_entries_from_table_soft_delete(self):
        table_name = "__test_tmp_table__"

        table, values = self._populate_db_for_drop_duplicate_entries(
            self.engine, self.meta, table_name)
        utils.drop_old_duplicate_entries_from_table(self.engine, table_name,
                                                    True, 'b', 'c')
        uniq_values = set()
        expected_values = []
        soft_deleted_values = []

        for value in sorted(values, key=lambda x: x['id'], reverse=True):
            uniq_value = (('b', value['b']), ('c', value['c']))
            if uniq_value in uniq_values:
                soft_deleted_values.append(value)
                continue
            uniq_values.add(uniq_value)
            expected_values.append(value)

        base_select = table.select()

        rows_select = base_select.where(table.c.deleted != table.c.id)
        row_ids = [row['id'] for row in
                   self.engine.execute(rows_select).fetchall()]
        self.assertEqual(len(row_ids), len(expected_values))
        for value in expected_values:
            self.assertTrue(value['id'] in row_ids)

        deleted_rows_select = base_select.where(
            table.c.deleted == table.c.id)
        deleted_rows_ids = [row['id'] for row in
                            self.engine.execute(
                                deleted_rows_select).fetchall()]
        self.assertEqual(len(deleted_rows_ids),
                         len(values) - len(row_ids))
        for value in soft_deleted_values:
            self.assertTrue(value['id'] in deleted_rows_ids)

    def test_change_deleted_column_type_does_not_drop_index(self):
        table_name = 'abc'

        indexes = {
            'idx_a_deleted': ['a', 'deleted'],
            'idx_b_deleted': ['b', 'deleted'],
            'idx_a': ['a']
        }

        index_instances = [Index(name, *columns)
                           for name, columns in six.iteritems(indexes)]

        table = Table(table_name, self.meta,
                      Column('id', Integer, primary_key=True),
                      Column('a', String(255)),
                      Column('b', String(255)),
                      Column('deleted', Boolean),
                      *index_instances)
        table.create()
        utils.change_deleted_column_type_to_id_type(self.engine, table_name)
        utils.change_deleted_column_type_to_boolean(self.engine, table_name)

        insp = reflection.Inspector.from_engine(self.engine)
        real_indexes = insp.get_indexes(table_name)
        self.assertEqual(len(real_indexes), 3)
        for index in real_indexes:
            name = index['name']
            self.assertIn(name, indexes)
            self.assertEqual(set(index['column_names']),
                             set(indexes[name]))

    def test_change_deleted_column_type_to_id_type_integer(self):
        table_name = 'abc'
        table = Table(table_name, self.meta,
                      Column('id', Integer, primary_key=True),
                      Column('deleted', Boolean))
        table.create()
        utils.change_deleted_column_type_to_id_type(self.engine, table_name)

        table = utils.get_table(self.engine, table_name)
        self.assertTrue(isinstance(table.c.deleted.type, Integer))

    def test_change_deleted_column_type_to_id_type_string(self):
        table_name = 'abc'
        table = Table(table_name, self.meta,
                      Column('id', String(255), primary_key=True),
                      Column('deleted', Boolean))
        table.create()
        utils.change_deleted_column_type_to_id_type(self.engine, table_name)

        table = utils.get_table(self.engine, table_name)
        self.assertTrue(isinstance(table.c.deleted.type, String))

    @db_test_base.backend_specific('sqlite')
    def test_change_deleted_column_type_to_id_type_custom(self):
        table_name = 'abc'
        table = Table(table_name, self.meta,
                      Column('id', Integer, primary_key=True),
                      Column('foo', CustomType),
                      Column('deleted', Boolean))
        table.create()

        # reflection of custom types has been fixed upstream
        if SA_VERSION < (0, 9, 0):
            self.assertRaises(exception.ColumnError,
                              utils.change_deleted_column_type_to_id_type,
                              self.engine, table_name)

        fooColumn = Column('foo', CustomType())
        utils.change_deleted_column_type_to_id_type(self.engine, table_name,
                                                    foo=fooColumn)

        table = utils.get_table(self.engine, table_name)
        # NOTE(boris-42): There is no way to check has foo type CustomType.
        #                 but sqlalchemy will set it to NullType. This has
        #                 been fixed upstream in recent SA versions
        if SA_VERSION < (0, 9, 0):
            self.assertTrue(isinstance(table.c.foo.type, NullType))
        self.assertTrue(isinstance(table.c.deleted.type, Integer))

    def test_change_deleted_column_type_to_boolean(self):
        table_name = 'abc'
        table = Table(table_name, self.meta,
                      Column('id', Integer, primary_key=True),
                      Column('deleted', Integer))
        table.create()

        utils.change_deleted_column_type_to_boolean(self.engine, table_name)

        table = utils.get_table(self.engine, table_name)
        if self.engine.name != "mysql":
            expected_type = Boolean
        else:
            expected_type = mysql.TINYINT

        self.assertTrue(isinstance(table.c.deleted.type, expected_type))

    def test_change_deleted_column_type_to_boolean_with_fc(self):
        table_name_1 = 'abc'
        table_name_2 = 'bcd'

        table_1 = Table(table_name_1, self.meta,
                        Column('id', Integer, primary_key=True),
                        Column('deleted', Integer))
        table_1.create()

        table_2 = Table(table_name_2, self.meta,
                        Column('id', Integer, primary_key=True),
                        Column('foreign_id', Integer,
                               ForeignKey('%s.id' % table_name_1)),
                        Column('deleted', Integer))
        table_2.create()

        utils.change_deleted_column_type_to_boolean(self.engine, table_name_2)

        table = utils.get_table(self.engine, table_name_2)
        if self.engine.name != "mysql":
            expected_type = Boolean
        else:
            expected_type = mysql.TINYINT

        self.assertTrue(isinstance(table.c.deleted.type, expected_type))

    @db_test_base.backend_specific('sqlite')
    def test_change_deleted_column_type_to_boolean_type_custom(self):
        table_name = 'abc'
        table = Table(table_name, self.meta,
                      Column('id', Integer, primary_key=True),
                      Column('foo', CustomType),
                      Column('deleted', Integer))
        table.create()

        # reflection of custom types has been fixed upstream
        if SA_VERSION < (0, 9, 0):
            self.assertRaises(exception.ColumnError,
                              utils.change_deleted_column_type_to_boolean,
                              self.engine, table_name)

        fooColumn = Column('foo', CustomType())
        utils.change_deleted_column_type_to_boolean(self.engine, table_name,
                                                    foo=fooColumn)

        table = utils.get_table(self.engine, table_name)
        # NOTE(boris-42): There is no way to check has foo type CustomType.
        #                 but sqlalchemy will set it to NullType. This has
        #                 been fixed upstream in recent SA versions
        if SA_VERSION < (0, 9, 0):
            self.assertTrue(isinstance(table.c.foo.type, NullType))
        self.assertTrue(isinstance(table.c.deleted.type, Boolean))

    @db_test_base.backend_specific('sqlite')
    def test_change_deleted_column_type_sqlite_drops_check_constraint(self):
        table_name = 'abc'
        table = Table(table_name, self.meta,
                      Column('id', Integer, primary_key=True),
                      Column('deleted', Boolean))
        table.create()

        utils._change_deleted_column_type_to_id_type_sqlite(self.engine,
                                                            table_name)
        table = Table(table_name, self.meta, autoload=True)
        # NOTE(I159): if the CHECK constraint has been dropped (expected
        # behavior), any integer value can be inserted, otherwise only 1 or 0.
        self.engine.execute(table.insert({'deleted': 10}))

    def test_utils_drop_unique_constraint(self):
        table_name = "__test_tmp_table__"
        uc_name = 'uniq_foo'
        values = [
            {'id': 1, 'a': 3, 'foo': 10},
            {'id': 2, 'a': 2, 'foo': 20},
            {'id': 3, 'a': 1, 'foo': 30},
        ]
        test_table = Table(
            table_name, self.meta,
            Column('id', Integer, primary_key=True, nullable=False),
            Column('a', Integer),
            Column('foo', Integer),
            UniqueConstraint('a', name='uniq_a'),
            UniqueConstraint('foo', name=uc_name),
        )
        test_table.create()

        self.engine.execute(test_table.insert(), values)
        # NOTE(boris-42): This method is generic UC dropper.
        utils.drop_unique_constraint(self.engine, table_name, uc_name, 'foo')

        s = test_table.select().order_by(test_table.c.id)
        rows = self.engine.execute(s).fetchall()

        for i in moves.range(len(values)):
            v = values[i]
            self.assertEqual((v['id'], v['a'], v['foo']), rows[i])

        # NOTE(boris-42): Update data about Table from DB.
        meta = MetaData(bind=self.engine)
        test_table = Table(table_name, meta, autoload=True)
        constraints = [c for c in test_table.constraints
                       if c.name == uc_name]
        self.assertEqual(len(constraints), 0)
        self.assertEqual(len(test_table.constraints), 1)

        test_table.drop()

    @db_test_base.backend_specific('sqlite')
    def test_util_drop_unique_constraint_with_not_supported_sqlite_type(self):
        table_name = "__test_tmp_table__"
        uc_name = 'uniq_foo'
        values = [
            {'id': 1, 'a': 3, 'foo': 10},
            {'id': 2, 'a': 2, 'foo': 20},
            {'id': 3, 'a': 1, 'foo': 30}
        ]

        test_table = Table(
            table_name, self.meta,
            Column('id', Integer, primary_key=True, nullable=False),
            Column('a', Integer),
            Column('foo', CustomType, default=0),
            UniqueConstraint('a', name='uniq_a'),
            UniqueConstraint('foo', name=uc_name),
        )
        test_table.create()

        self.engine.execute(test_table.insert(), values)
        warnings.simplefilter("ignore", SAWarning)

        # reflection of custom types has been fixed upstream
        if SA_VERSION < (0, 9, 0):
            # NOTE(boris-42): Missing info about column `foo` that has
            #                 unsupported type CustomType.
            self.assertRaises(exception.ColumnError,
                              utils.drop_unique_constraint,
                              self.engine, table_name, uc_name, 'foo')

            # NOTE(boris-42): Wrong type of foo instance. it should be
            #                 instance of sqlalchemy.Column.
            self.assertRaises(exception.ColumnError,
                              utils.drop_unique_constraint,
                              self.engine, table_name, uc_name, 'foo',
                              foo=Integer())

        foo = Column('foo', CustomType, default=0)
        utils.drop_unique_constraint(
            self.engine, table_name, uc_name, 'foo', foo=foo)

        s = test_table.select().order_by(test_table.c.id)
        rows = self.engine.execute(s).fetchall()

        for i in moves.range(len(values)):
            v = values[i]
            self.assertEqual((v['id'], v['a'], v['foo']), rows[i])

        # NOTE(boris-42): Update data about Table from DB.
        meta = MetaData(bind=self.engine)
        test_table = Table(table_name, meta, autoload=True)
        constraints = [c for c in test_table.constraints if c.name == uc_name]
        self.assertEqual(len(constraints), 0)
        self.assertEqual(len(test_table.constraints), 1)
        test_table.drop()

    @db_test_base.backend_specific('sqlite')
    def test_drop_unique_constraint_in_sqlite_fk_recreate(self):
        parent_table = Table(
            'table0', self.meta,
            Column('id', Integer, primary_key=True),
            Column('foo', Integer),
        )
        parent_table.create()
        table_name = 'table1'
        table = Table(
            table_name, self.meta,
            Column('id', Integer, primary_key=True),
            Column('baz', Integer),
            Column('bar', Integer, ForeignKey("table0.id")),
            UniqueConstraint('baz', name='constr1')
        )
        table.create()
        utils.drop_unique_constraint(self.engine, table_name, 'constr1', 'baz')

        insp = reflection.Inspector.from_engine(self.engine)
        f_keys = insp.get_foreign_keys(table_name)
        self.assertEqual(len(f_keys), 1)
        f_key = f_keys[0]
        self.assertEqual(f_key['referred_table'], 'table0')
        self.assertEqual(f_key['referred_columns'], ['id'])
        self.assertEqual(f_key['constrained_columns'], ['bar'])

    def test_insert_from_select(self):
        insert_table_name = "__test_insert_to_table__"
        select_table_name = "__test_select_from_table__"
        uuidstrs = []
        for unused in range(10):
            uuidstrs.append(uuid.uuid4().hex)
        insert_table = Table(
            insert_table_name, self.meta,
            Column('id', Integer, primary_key=True,
                   nullable=False, autoincrement=True),
            Column('uuid', String(36), nullable=False))
        select_table = Table(
            select_table_name, self.meta,
            Column('id', Integer, primary_key=True,
                   nullable=False, autoincrement=True),
            Column('uuid', String(36), nullable=False))

        insert_table.create()
        select_table.create()
        # Add 10 rows to select_table
        for uuidstr in uuidstrs:
            ins_stmt = select_table.insert().values(uuid=uuidstr)
            self.conn.execute(ins_stmt)

        # Select 4 rows in one chunk from select_table
        column = select_table.c.id
        query_insert = select([select_table],
                              select_table.c.id < 5).order_by(column)
        insert_statement = utils.InsertFromSelect(insert_table,
                                                  query_insert)
        result_insert = self.conn.execute(insert_statement)
        # Verify we insert 4 rows
        self.assertEqual(result_insert.rowcount, 4)

        query_all = select([insert_table]).where(
            insert_table.c.uuid.in_(uuidstrs))
        rows = self.conn.execute(query_all).fetchall()
        # Verify we really have 4 rows in insert_table
        self.assertEqual(len(rows), 4)


class PostgesqlTestMigrations(TestMigrationUtils,
                              db_test_base.PostgreSQLOpportunisticTestCase):

    """Test migrations on PostgreSQL."""
    pass


class MySQLTestMigrations(TestMigrationUtils,
                          db_test_base.MySQLOpportunisticTestCase):

    """Test migrations on MySQL."""
    pass


class TestConnectionUtils(test_utils.BaseTestCase):

    def setUp(self):
        super(TestConnectionUtils, self).setUp()

        self.full_credentials = {'backend': 'mysql',
                                 'database': 'test',
                                 'user': 'dude',
                                 'passwd': 'pass'}

        self.connect_string = 'mysql://dude:pass@localhost/test'

    def test_connect_string(self):
        connect_string = utils.get_connect_string(**self.full_credentials)
        self.assertEqual(connect_string, self.connect_string)

    def test_connect_string_sqlite(self):
        sqlite_credentials = {'backend': 'sqlite', 'database': 'test.db'}
        connect_string = utils.get_connect_string(**sqlite_credentials)
        self.assertEqual(connect_string, 'sqlite:///test.db')

    def test_is_backend_avail(self):
        self.mox.StubOutWithMock(sqlalchemy.engine.base.Engine, 'connect')
        fake_connection = self.mox.CreateMockAnything()
        fake_connection.close()
        sqlalchemy.engine.base.Engine.connect().AndReturn(fake_connection)
        self.mox.ReplayAll()

        self.assertTrue(utils.is_backend_avail(**self.full_credentials))

    def test_is_backend_unavail(self):
        log = self.useFixture(fixtures.FakeLogger())
        error_cause = ('This result object does not return rows. It has been'
                       'closed automatically.')
        error_msg = ("The %s backend is unavailable: %s\n" %
                     ('mysql', error_cause))

        self.mox.StubOutWithMock(sqlalchemy.engine.base.Engine, 'connect')
        sqlalchemy.engine.base.Engine.connect().AndRaise(
            ResourceClosedError(error_cause))
        self.mox.ReplayAll()

        self.assertFalse(utils.is_backend_avail(**self.full_credentials))
        self.assertEqual(error_msg, log.output)

    def test_get_db_connection_info(self):
        conn_pieces = parse.urlparse(self.connect_string)
        self.assertEqual(utils.get_db_connection_info(conn_pieces),
                         ('dude', 'pass', 'test', 'localhost'))

    def test_connect_string_host(self):
        self.full_credentials['host'] = 'myhost'
        connect_string = utils.get_connect_string(**self.full_credentials)
        self.assertEqual(connect_string, 'mysql://dude:pass@myhost/test')


class MyModelSoftDeletedProjectId(declarative_base(), models.ModelBase,
                                  models.SoftDeleteMixin):
    __tablename__ = 'soft_deleted_project_id_test_model'
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer)


class MyModel(declarative_base(), models.ModelBase):
    __tablename__ = 'test_model'
    id = Column(Integer, primary_key=True)


class MyModelSoftDeleted(declarative_base(), models.ModelBase,
                         models.SoftDeleteMixin):
    __tablename__ = 'soft_deleted_test_model'
    id = Column(Integer, primary_key=True)


class TestModelQuery(test_base.BaseTestCase):

    def setUp(self):
        super(TestModelQuery, self).setUp()

        self.session = mock.MagicMock()
        self.session.query.return_value = self.session.query
        self.session.query.filter.return_value = self.session.query

    def test_wrong_model(self):
        self.assertRaises(TypeError, utils.model_query,
                          FakeModel, session=self.session)

    def test_no_soft_deleted(self):
        self.assertRaises(ValueError, utils.model_query,
                          MyModel, session=self.session, deleted=True)

    def test_deleted_false(self):
        mock_query = utils.model_query(
            MyModelSoftDeleted, session=self.session, deleted=False)

        deleted_filter = mock_query.filter.call_args[0][0]
        self.assertEqual(str(deleted_filter),
                         'soft_deleted_test_model.deleted = :deleted_1')
        self.assertEqual(deleted_filter.right.value,
                         MyModelSoftDeleted.__mapper__.c.deleted.default.arg)

    def test_deleted_true(self):
        mock_query = utils.model_query(
            MyModelSoftDeleted, session=self.session, deleted=True)

        deleted_filter = mock_query.filter.call_args[0][0]
        self.assertEqual(str(deleted_filter),
                         'soft_deleted_test_model.deleted != :deleted_1')
        self.assertEqual(deleted_filter.right.value,
                         MyModelSoftDeleted.__mapper__.c.deleted.default.arg)

    @mock.patch.object(utils, "_read_deleted_filter")
    def test_no_deleted_value(self, _read_deleted_filter):
        utils.model_query(MyModelSoftDeleted, session=self.session)
        self.assertEqual(_read_deleted_filter.call_count, 0)

    def test_project_filter(self):
        project_id = 10

        mock_query = utils.model_query(
            MyModelSoftDeletedProjectId, session=self.session,
            project_only=True, project_id=project_id)

        deleted_filter = mock_query.filter.call_args[0][0]
        self.assertEqual(
            str(deleted_filter),
            'soft_deleted_project_id_test_model.project_id = :project_id_1')
        self.assertEqual(deleted_filter.right.value, project_id)

    def test_project_filter_wrong_model(self):
        self.assertRaises(ValueError, utils.model_query,
                          MyModelSoftDeleted, session=self.session,
                          project_id=10)

    def test_project_filter_allow_none(self):
        mock_query = utils.model_query(
            MyModelSoftDeletedProjectId,
            session=self.session, project_id=(10, None))

        self.assertEqual(
            str(mock_query.filter.call_args[0][0]),
            'soft_deleted_project_id_test_model.project_id'
            ' IN (:project_id_1, NULL)'
        )

    def test_model_query_common(self):
        utils.model_query(MyModel, args=(MyModel.id,), session=self.session)
        self.session.query.assert_called_with(MyModel.id)


class TestUtils(db_test_base.DbTestCase):
    def setUp(self):
        super(TestUtils, self).setUp()
        meta = MetaData(bind=self.engine)
        self.test_table = Table(
            'test_table',
            meta,
            Column('a', Integer),
            Column('b', Integer)
        )
        self.test_table.create()
        self.addCleanup(meta.drop_all)

    def test_index_exists(self):
        self.assertFalse(utils.index_exists(self.engine, 'test_table',
                                            'new_index'))
        Index('new_index', self.test_table.c.a).create(self.engine)
        self.assertTrue(utils.index_exists(self.engine, 'test_table',
                                           'new_index'))

    def test_add_index(self):
        self.assertFalse(utils.index_exists(self.engine, 'test_table',
                                            'new_index'))
        utils.add_index(self.engine, 'test_table', 'new_index', ('a',))
        self.assertTrue(utils.index_exists(self.engine, 'test_table',
                                           'new_index'))

    def test_add_existing_index(self):
        Index('new_index', self.test_table.c.a).create(self.engine)
        self.assertRaises(ValueError, utils.add_index, self.engine,
                          'test_table', 'new_index', ('a',))

    def test_drop_index(self):
        Index('new_index', self.test_table.c.a).create(self.engine)
        utils.drop_index(self.engine, 'test_table', 'new_index')
        self.assertFalse(utils.index_exists(self.engine, 'test_table',
                         'new_index'))

    def test_drop_unexisting_index(self):
        self.assertRaises(ValueError, utils.drop_index, self.engine,
                          'test_table', 'new_index')

    @mock.patch('oslo.db.sqlalchemy.utils.drop_index')
    @mock.patch('oslo.db.sqlalchemy.utils.add_index')
    def test_change_index_columns(self, add_index, drop_index):
        utils.change_index_columns(self.engine, 'test_table', 'a_index',
                                  ('a',))
        utils.drop_index.assert_called_once_with(self.engine, 'test_table',
                                                 'a_index')
        utils.add_index.assert_called_once_with(self.engine, 'test_table',
                                                'a_index', ('a',))

    def test_column_exists(self):
        for col in ['a', 'b']:
            self.assertTrue(utils.column_exists(self.engine, 'test_table',
                                                col))
        self.assertFalse(utils.column_exists(self.engine, 'test_table',
                                             'fake_column'))


class TestUtilsMysqlOpportunistically(
        TestUtils, db_test_base.MySQLOpportunisticTestCase):
    pass


class TestUtilsPostgresqlOpportunistically(
        TestUtils, db_test_base.PostgreSQLOpportunisticTestCase):
    pass
