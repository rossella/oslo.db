# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2010-2011 OpenStack Foundation.
# Copyright 2012 Justin Santa Barbara
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

import logging
import re

import sqlalchemy
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy.engine import reflection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import func
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy.sql.expression import literal_column
from sqlalchemy.sql.expression import UpdateBase
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy.types import NullType

from oslo.db import exception
from oslo.db.openstack.common.gettextutils import _, _LI, _LW
from oslo.db.openstack.common import timeutils
from oslo.db.sqlalchemy import models

# NOTE(ochuprykov): Add references for backwards compatibility
InvalidSortKey = exception.InvalidSortKey
ColumnError = exception.ColumnError

LOG = logging.getLogger(__name__)

_DBURL_REGEX = re.compile(r"[^:]+://([^:]+):([^@]+)@.+")


def sanitize_db_url(url):
    match = _DBURL_REGEX.match(url)
    if match:
        return '%s****:****%s' % (url[:match.start(1)], url[match.end(2):])
    return url


# copy from glance/db/sqlalchemy/api.py
def paginate_query(query, model, limit, sort_keys, marker=None,
                   sort_dir=None, sort_dirs=None):
    """Returns a query with sorting / pagination criteria added.

    Pagination works by requiring a unique sort_key, specified by sort_keys.
    (If sort_keys is not unique, then we risk looping through values.)
    We use the last row in the previous page as the 'marker' for pagination.
    So we must return values that follow the passed marker in the order.
    With a single-valued sort_key, this would be easy: sort_key > X.
    With a compound-values sort_key, (k1, k2, k3) we must do this to repeat
    the lexicographical ordering:
    (k1 > X1) or (k1 == X1 && k2 > X2) or (k1 == X1 && k2 == X2 && k3 > X3)

    We also have to cope with different sort_directions.

    Typically, the id of the last row is used as the client-facing pagination
    marker, then the actual marker object must be fetched from the db and
    passed in to us as marker.

    :param query: the query object to which we should add paging/sorting
    :param model: the ORM model class
    :param limit: maximum number of items to return
    :param sort_keys: array of attributes by which results should be sorted
    :param marker: the last item of the previous page; we returns the next
                    results after this value.
    :param sort_dir: direction in which results should be sorted (asc, desc)
    :param sort_dirs: per-column array of sort_dirs, corresponding to sort_keys

    :rtype: sqlalchemy.orm.query.Query
    :return: The query with sorting/pagination added.
    """

    if 'id' not in sort_keys:
        # TODO(justinsb): If this ever gives a false-positive, check
        # the actual primary key, rather than assuming its id
        LOG.warning(_LW('Id not in sort_keys; is sort_keys unique?'))

    assert(not (sort_dir and sort_dirs))

    # Default the sort direction to ascending
    if sort_dirs is None and sort_dir is None:
        sort_dir = 'asc'

    # Ensure a per-column sort direction
    if sort_dirs is None:
        sort_dirs = [sort_dir for _sort_key in sort_keys]

    assert(len(sort_dirs) == len(sort_keys))

    # Add sorting
    for current_sort_key, current_sort_dir in zip(sort_keys, sort_dirs):
        try:
            sort_dir_func = {
                'asc': sqlalchemy.asc,
                'desc': sqlalchemy.desc,
            }[current_sort_dir]
        except KeyError:
            raise ValueError(_("Unknown sort direction, "
                               "must be 'desc' or 'asc'"))
        try:
            sort_key_attr = getattr(model, current_sort_key)
        except AttributeError:
            raise exception.InvalidSortKey()
        query = query.order_by(sort_dir_func(sort_key_attr))

    # Add pagination
    if marker is not None:
        marker_values = []
        for sort_key in sort_keys:
            v = getattr(marker, sort_key)
            marker_values.append(v)

        # Build up an array of sort criteria as in the docstring
        criteria_list = []
        for i in range(len(sort_keys)):
            crit_attrs = []
            for j in range(i):
                model_attr = getattr(model, sort_keys[j])
                crit_attrs.append((model_attr == marker_values[j]))

            model_attr = getattr(model, sort_keys[i])
            if sort_dirs[i] == 'desc':
                crit_attrs.append((model_attr < marker_values[i]))
            else:
                crit_attrs.append((model_attr > marker_values[i]))

            criteria = sqlalchemy.sql.and_(*crit_attrs)
            criteria_list.append(criteria)

        f = sqlalchemy.sql.or_(*criteria_list)
        query = query.filter(f)

    if limit is not None:
        query = query.limit(limit)

    return query


def _read_deleted_filter(query, db_model, deleted):
    if 'deleted' not in db_model.__table__.columns:
        raise ValueError(_("There is no `deleted` column in `%s` table. "
                           "Project doesn't use soft-deleted feature.")
                         % db_model.__name__)

    default_deleted_value = db_model.__table__.c.deleted.default.arg
    if deleted:
        query = query.filter(db_model.deleted != default_deleted_value)
    else:
        query = query.filter(db_model.deleted == default_deleted_value)
    return query


def _project_filter(query, db_model, project_id):
    if 'project_id' not in db_model.__table__.columns:
        raise ValueError(_("There is no `project_id` column in `%s` table.")
                         % db_model.__name__)

    if isinstance(project_id, (list, tuple, set)):
        query = query.filter(db_model.project_id.in_(project_id))
    else:
        query = query.filter(db_model.project_id == project_id)

    return query


def model_query(model, session, args=None, **kwargs):
    """Query helper for db.sqlalchemy api methods.

    This accounts for `deleted` and `project_id` fields.

    :param model:        Model to query. Must be a subclass of ModelBase.
    :type model:         models.ModelBase

    :param session:      The session to use.
    :type session:       sqlalchemy.orm.session.Session

    :param args:         Arguments to query. If None - model is used.
    :type args:          tuple

    Keyword arguments:

    :keyword project_id: If present, allows filtering by project_id(s).
                         Can be either a project_id value, or an iterable of
                         project_id values, or None. If an iterable is passed,
                         only rows whose project_id column value is on the
                         `project_id` list will be returned. If None is passed,
                         only rows which are not bound to any project, will be
                         returned.
    :type project_id:    iterable,
                         model.__table__.columns.project_id.type,
                         None type

    :keyword deleted:    If present, allows filtering by deleted field.
                         If True is passed, only deleted entries will be
                         returned, if False - only existing entries.
    :type deleted:       bool


    Usage:

    .. code-block:: python

      from oslo.db.sqlalchemy import utils


      def get_instance_by_uuid(uuid):
          session = get_session()
          with session.begin()
              return (utils.model_query(models.Instance, session=session)
                           .filter(models.Instance.uuid == uuid)
                           .first())

      def get_nodes_stat():
          data = (Node.id, Node.cpu, Node.ram, Node.hdd)

          session = get_session()
          with session.begin()
              return utils.model_query(Node, session=session, args=data).all()

    Also you can create your own helper, based on ``utils.model_query()``.
    For example, it can be useful if you plan to use ``project_id`` and
    ``deleted`` parameters from project's ``context``

    .. code-block:: python

      from oslo.db.sqlalchemy import utils


      def _model_query(context, model, session=None, args=None,
                       project_id=None, project_only=False,
                       read_deleted=None):

          # We suppose, that functions ``_get_project_id()`` and
          # ``_get_deleted()`` should handle passed parameters and
          # context object (for example, decide, if we need to restrict a user
          # to query his own entries by project_id or only allow admin to read
          # deleted entries). For return values, we expect to get
          # ``project_id`` and ``deleted``, which are suitable for the
          # ``model_query()`` signature.
          kwargs = {}
          if project_id is not None:
              kwargs['project_id'] = _get_project_id(context, project_id,
                                                     project_only)
          if read_deleted is not None:
              kwargs['deleted'] = _get_deleted_dict(context, read_deleted)
          session = session or get_session()

          with session.begin():
              return utils.model_query(model, session=session,
                                       args=args, **kwargs)

      def get_instance_by_uuid(context, uuid):
          return (_model_query(context, models.Instance, read_deleted='yes')
                        .filter(models.Instance.uuid == uuid)
                        .first())

      def get_nodes_data(context, project_id, project_only='allow_none'):
          data = (Node.id, Node.cpu, Node.ram, Node.hdd)

          return (_model_query(context, Node, args=data, project_id=project_id,
                               project_only=project_only)
                        .all())

    """

    if not issubclass(model, models.ModelBase):
        raise TypeError(_("model should be a subclass of ModelBase"))

    query = session.query(model) if not args else session.query(*args)
    if 'deleted' in kwargs:
        query = _read_deleted_filter(query, model, kwargs['deleted'])
    if 'project_id' in kwargs:
        query = _project_filter(query, model, kwargs['project_id'])

    return query


def get_table(engine, name):
    """Returns an sqlalchemy table dynamically from db.

    Needed because the models don't work for us in migrations
    as models will be far out of sync with the current data.

    .. warning::

       Do not use this method when creating ForeignKeys in database migrations
       because sqlalchemy needs the same MetaData object to hold information
       about the parent table and the reference table in the ForeignKey. This
       method uses a unique MetaData object per table object so it won't work
       with ForeignKey creation.
    """
    metadata = MetaData()
    metadata.bind = engine
    return Table(name, metadata, autoload=True)


class InsertFromSelect(UpdateBase):
    """Form the base for `INSERT INTO table (SELECT ... )` statement."""
    def __init__(self, table, select):
        self.table = table
        self.select = select


@compiles(InsertFromSelect)
def visit_insert_from_select(element, compiler, **kw):
    """Form the `INSERT INTO table (SELECT ... )` statement."""
    return "INSERT INTO %s %s" % (
        compiler.process(element.table, asfrom=True),
        compiler.process(element.select))


def _get_not_supported_column(col_name_col_instance, column_name):
    try:
        column = col_name_col_instance[column_name]
    except KeyError:
        msg = _("Please specify column %s in col_name_col_instance "
                "param. It is required because column has unsupported "
                "type by SQLite.")
        raise exception.ColumnError(msg % column_name)

    if not isinstance(column, Column):
        msg = _("col_name_col_instance param has wrong type of "
                "column instance for column %s It should be instance "
                "of sqlalchemy.Column.")
        raise exception.ColumnError(msg % column_name)
    return column


def drop_unique_constraint(migrate_engine, table_name, uc_name, *columns,
                           **col_name_col_instance):
    """Drop unique constraint from table.

    DEPRECATED: this function is deprecated and will be removed from oslo.db
    in a few releases. Please use UniqueConstraint.drop() method directly for
    sqlalchemy-migrate migration scripts.

    This method drops UC from table and works for mysql, postgresql and sqlite.
    In mysql and postgresql we are able to use "alter table" construction.
    Sqlalchemy doesn't support some sqlite column types and replaces their
    type with NullType in metadata. We process these columns and replace
    NullType with the correct column type.

    :param migrate_engine: sqlalchemy engine
    :param table_name:     name of table that contains uniq constraint.
    :param uc_name:        name of uniq constraint that will be dropped.
    :param columns:        columns that are in uniq constraint.
    :param col_name_col_instance:   contains pair column_name=column_instance.
                            column_instance is instance of Column. These params
                            are required only for columns that have unsupported
                            types by sqlite. For example BigInteger.
    """

    from migrate.changeset import UniqueConstraint

    meta = MetaData()
    meta.bind = migrate_engine
    t = Table(table_name, meta, autoload=True)

    if migrate_engine.name == "sqlite":
        override_cols = [
            _get_not_supported_column(col_name_col_instance, col.name)
            for col in t.columns
            if isinstance(col.type, NullType)
        ]
        for col in override_cols:
            t.columns.replace(col)

    uc = UniqueConstraint(*columns, table=t, name=uc_name)
    uc.drop()


def drop_old_duplicate_entries_from_table(migrate_engine, table_name,
                                          use_soft_delete, *uc_column_names):
    """Drop all old rows having the same values for columns in uc_columns.

    This method drop (or mark ad `deleted` if use_soft_delete is True) old
    duplicate rows form table with name `table_name`.

    :param migrate_engine:  Sqlalchemy engine
    :param table_name:      Table with duplicates
    :param use_soft_delete: If True - values will be marked as `deleted`,
                            if False - values will be removed from table
    :param uc_column_names: Unique constraint columns
    """
    meta = MetaData()
    meta.bind = migrate_engine

    table = Table(table_name, meta, autoload=True)
    columns_for_group_by = [table.c[name] for name in uc_column_names]

    columns_for_select = [func.max(table.c.id)]
    columns_for_select.extend(columns_for_group_by)

    duplicated_rows_select = sqlalchemy.sql.select(
        columns_for_select, group_by=columns_for_group_by,
        having=func.count(table.c.id) > 1)

    for row in migrate_engine.execute(duplicated_rows_select).fetchall():
        # NOTE(boris-42): Do not remove row that has the biggest ID.
        delete_condition = table.c.id != row[0]
        is_none = None  # workaround for pyflakes
        delete_condition &= table.c.deleted_at == is_none
        for name in uc_column_names:
            delete_condition &= table.c[name] == row[name]

        rows_to_delete_select = sqlalchemy.sql.select(
            [table.c.id]).where(delete_condition)
        for row in migrate_engine.execute(rows_to_delete_select).fetchall():
            LOG.info(_LI("Deleting duplicated row with id: %(id)s from table: "
                         "%(table)s"), dict(id=row[0], table=table_name))

        if use_soft_delete:
            delete_statement = table.update().\
                where(delete_condition).\
                values({
                    'deleted': literal_column('id'),
                    'updated_at': literal_column('updated_at'),
                    'deleted_at': timeutils.utcnow()
                })
        else:
            delete_statement = table.delete().where(delete_condition)
        migrate_engine.execute(delete_statement)


def _get_default_deleted_value(table):
    if isinstance(table.c.id.type, Integer):
        return 0
    if isinstance(table.c.id.type, String):
        return ""
    raise exception.ColumnError(_("Unsupported id columns type"))


def _restore_indexes_on_deleted_columns(migrate_engine, table_name, indexes):
    table = get_table(migrate_engine, table_name)

    insp = reflection.Inspector.from_engine(migrate_engine)
    real_indexes = insp.get_indexes(table_name)
    existing_index_names = dict(
        [(index['name'], index['column_names']) for index in real_indexes])

    # NOTE(boris-42): Restore indexes on `deleted` column
    for index in indexes:
        if 'deleted' not in index['column_names']:
            continue
        name = index['name']
        if name in existing_index_names:
            column_names = [table.c[c] for c in existing_index_names[name]]
            old_index = Index(name, *column_names, unique=index["unique"])
            old_index.drop(migrate_engine)

        column_names = [table.c[c] for c in index['column_names']]
        new_index = Index(index["name"], *column_names, unique=index["unique"])
        new_index.create(migrate_engine)


def change_deleted_column_type_to_boolean(migrate_engine, table_name,
                                          **col_name_col_instance):
    if migrate_engine.name == "sqlite":
        return _change_deleted_column_type_to_boolean_sqlite(
            migrate_engine, table_name, **col_name_col_instance)
    insp = reflection.Inspector.from_engine(migrate_engine)
    indexes = insp.get_indexes(table_name)

    table = get_table(migrate_engine, table_name)

    old_deleted = Column('old_deleted', Boolean, default=False)
    old_deleted.create(table, populate_default=False)

    table.update().\
        where(table.c.deleted == table.c.id).\
        values(old_deleted=True).\
        execute()

    table.c.deleted.drop()
    table.c.old_deleted.alter(name="deleted")

    _restore_indexes_on_deleted_columns(migrate_engine, table_name, indexes)


def _change_deleted_column_type_to_boolean_sqlite(migrate_engine, table_name,
                                                  **col_name_col_instance):
    insp = reflection.Inspector.from_engine(migrate_engine)
    table = get_table(migrate_engine, table_name)

    columns = []
    for column in table.columns:
        column_copy = None
        if column.name != "deleted":
            if isinstance(column.type, NullType):
                column_copy = _get_not_supported_column(col_name_col_instance,
                                                        column.name)
            else:
                column_copy = column.copy()
        else:
            column_copy = Column('deleted', Boolean, default=0)
        columns.append(column_copy)

    constraints = [constraint.copy() for constraint in table.constraints]

    meta = table.metadata
    new_table = Table(table_name + "__tmp__", meta,
                      *(columns + constraints))
    new_table.create()

    indexes = []
    for index in insp.get_indexes(table_name):
        column_names = [new_table.c[c] for c in index['column_names']]
        indexes.append(Index(index["name"], *column_names,
                             unique=index["unique"]))

    c_select = []
    for c in table.c:
        if c.name != "deleted":
            c_select.append(c)
        else:
            c_select.append(table.c.deleted == table.c.id)

    ins = InsertFromSelect(new_table, sqlalchemy.sql.select(c_select))
    migrate_engine.execute(ins)

    table.drop()
    for index in indexes:
        index.create(migrate_engine)

    new_table.rename(table_name)
    new_table.update().\
        where(new_table.c.deleted == new_table.c.id).\
        values(deleted=True).\
        execute()


def change_deleted_column_type_to_id_type(migrate_engine, table_name,
                                          **col_name_col_instance):
    if migrate_engine.name == "sqlite":
        return _change_deleted_column_type_to_id_type_sqlite(
            migrate_engine, table_name, **col_name_col_instance)
    insp = reflection.Inspector.from_engine(migrate_engine)
    indexes = insp.get_indexes(table_name)

    table = get_table(migrate_engine, table_name)

    new_deleted = Column('new_deleted', table.c.id.type,
                         default=_get_default_deleted_value(table))
    new_deleted.create(table, populate_default=True)

    deleted = True  # workaround for pyflakes
    table.update().\
        where(table.c.deleted == deleted).\
        values(new_deleted=table.c.id).\
        execute()
    table.c.deleted.drop()
    table.c.new_deleted.alter(name="deleted")

    _restore_indexes_on_deleted_columns(migrate_engine, table_name, indexes)


def _change_deleted_column_type_to_id_type_sqlite(migrate_engine, table_name,
                                                  **col_name_col_instance):
    # NOTE(boris-42): sqlaclhemy-migrate can't drop column with check
    #                 constraints in sqlite DB and our `deleted` column has
    #                 2 check constraints. So there is only one way to remove
    #                 these constraints:
    #                 1) Create new table with the same columns, constraints
    #                 and indexes. (except deleted column).
    #                 2) Copy all data from old to new table.
    #                 3) Drop old table.
    #                 4) Rename new table to old table name.
    insp = reflection.Inspector.from_engine(migrate_engine)
    meta = MetaData(bind=migrate_engine)
    table = Table(table_name, meta, autoload=True)
    default_deleted_value = _get_default_deleted_value(table)

    columns = []
    for column in table.columns:
        column_copy = None
        if column.name != "deleted":
            if isinstance(column.type, NullType):
                column_copy = _get_not_supported_column(col_name_col_instance,
                                                        column.name)
            else:
                column_copy = column.copy()
        else:
            column_copy = Column('deleted', table.c.id.type,
                                 default=default_deleted_value)
        columns.append(column_copy)

    def is_deleted_column_constraint(constraint):
        # NOTE(boris-42): There is no other way to check is CheckConstraint
        #                 associated with deleted column.
        if not isinstance(constraint, CheckConstraint):
            return False
        sqltext = str(constraint.sqltext)
        # NOTE(I159): in order to omit the CHECK constraint corresponding
        # to `deleted` column we have to test these patterns which may
        # vary depending on the SQLAlchemy version used.
        constraint_markers = (
            "deleted in (0, 1)",
            "deleted IN (:deleted_1, :deleted_2)",
            "deleted IN (:param_1, :param_2)"
        )
        return any(sqltext.endswith(marker) for marker in constraint_markers)

    constraints = []
    for constraint in table.constraints:
        if not is_deleted_column_constraint(constraint):
            constraints.append(constraint.copy())

    new_table = Table(table_name + "__tmp__", meta,
                      *(columns + constraints))
    new_table.create()

    indexes = []
    for index in insp.get_indexes(table_name):
        column_names = [new_table.c[c] for c in index['column_names']]
        indexes.append(Index(index["name"], *column_names,
                             unique=index["unique"]))

    ins = InsertFromSelect(new_table, table.select())
    migrate_engine.execute(ins)

    table.drop()
    for index in indexes:
        index.create(migrate_engine)

    new_table.rename(table_name)
    deleted = True  # workaround for pyflakes
    new_table.update().\
        where(new_table.c.deleted == deleted).\
        values(deleted=new_table.c.id).\
        execute()

    # NOTE(boris-42): Fix value of deleted column: False -> "" or 0.
    deleted = False  # workaround for pyflakes
    new_table.update().\
        where(new_table.c.deleted == deleted).\
        values(deleted=default_deleted_value).\
        execute()


def get_connect_string(backend, database, user=None, passwd=None,
                       host='localhost'):
    """Get database connection

    Try to get a connection with a very specific set of values, if we get
    these then we'll run the tests, otherwise they are skipped
    """
    args = {'backend': backend,
            'user': user,
            'passwd': passwd,
            'host': host,
            'database': database}
    if backend == 'sqlite':
        template = '%(backend)s:///%(database)s'
    else:
        template = "%(backend)s://%(user)s:%(passwd)s@%(host)s/%(database)s"
    return template % args


def is_backend_avail(backend, database, user=None, passwd=None):
    try:
        connect_uri = get_connect_string(backend=backend,
                                         database=database,
                                         user=user,
                                         passwd=passwd)
        engine = sqlalchemy.create_engine(connect_uri)
        connection = engine.connect()
    except Exception as e:
        # intentionally catch all to handle exceptions even if we don't
        # have any backend code loaded.
        LOG.info(_LI("The %s backend is unavailable: %s"), backend, e)
        return False
    else:
        connection.close()
        engine.dispose()
        return True


def get_db_connection_info(conn_pieces):
    database = conn_pieces.path.strip('/')
    loc_pieces = conn_pieces.netloc.split('@')
    host = loc_pieces[1]

    auth_pieces = loc_pieces[0].split(':')
    user = auth_pieces[0]
    password = ""
    if len(auth_pieces) > 1:
        password = auth_pieces[1].strip()

    return (user, password, database, host)


def index_exists(migrate_engine, table_name, index_name):
    """Check if given index exists.

    :param migrate_engine: sqlalchemy engine
    :param table_name:     name of the table
    :param index_name:     name of the index
    """
    inspector = reflection.Inspector.from_engine(migrate_engine)
    indexes = inspector.get_indexes(table_name)
    index_names = [index['name'] for index in indexes]
    return index_name in index_names


def add_index(migrate_engine, table_name, index_name, idx_columns):
    """Create an index for given columns.

    :param migrate_engine: sqlalchemy engine
    :param table_name:     name of the table
    :param index_name:     name of the index
    :param idx_columns:    tuple with names of columns that will be indexed
    """
    table = get_table(migrate_engine, table_name)
    if not index_exists(migrate_engine, table_name, index_name):
        index = Index(
            index_name, *[getattr(table.c, col) for col in idx_columns]
        )
        index.create()
    else:
        raise ValueError("Index '%s' already exists!" % index_name)


def drop_index(migrate_engine, table_name, index_name):
    """Drop index with given name.

    :param migrate_engine: sqlalchemy engine
    :param table_name:     name of the table
    :param index_name:     name of the index
    """
    table = get_table(migrate_engine, table_name)
    for index in table.indexes:
        if index.name == index_name:
            index.drop()
            break
    else:
        raise ValueError("Index '%s' not found!" % index_name)


def change_index_columns(migrate_engine, table_name, index_name, new_columns):
    """Change set of columns that are indexed by given index.

    :param migrate_engine: sqlalchemy engine
    :param table_name:     name of the table
    :param index_name:     name of the index
    :param new_columns:    tuple with names of columns that will be indexed
    """
    drop_index(migrate_engine, table_name, index_name)
    add_index(migrate_engine, table_name, index_name, new_columns)


def column_exists(engine, table_name, column):
    """Check if table has given column.

    :param engine:         sqlalchemy engine
    :param table_name:     name of the table
    :param column:         name of the colmn
    """
    t = get_table(engine, table_name)
    return column in t.c
