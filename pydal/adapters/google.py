import os
import re
import time
import uuid

from .._compat import pjoin
from .._globals import THREAD_LOCAL
from ..helpers.classes import SQLALL, FakeDriver, Reference, SQLCustomType
from ..helpers.methods import use_common_filters, xorify
from ..migrator import InDBMigrator
from ..objects import Expression, Field, Query, Table
from . import adapters, with_connection_or_raise
from .base import NoSQLAdapter
from .mysql import MySQL
from .postgres import PostgrePsyco


class GoogleMigratorMixin(object):
    migrator_cls = InDBMigrator


class Streamable:
    def __init__(self, item):
        self.item = item

    def stream(self):
        if self.item:
            yield self.item


@adapters.register_for("google:sql")
class GoogleSQL(GoogleMigratorMixin, MySQL):
    uploads_in_blob = True
    REGEX_URI = "^(?P<instance>.*)/(?P<db>.+)$"

    def _find_work_folder(self):
        super(GoogleSQL, self)._find_work_folder()
        if os.path.isabs(self.folder) and self.folder.startswith(os.getcwd()):
            self.folder = os.path.relpath(self.folder, os.getcwd())

    def _initialize_(self):
        super(GoogleSQL, self)._initialize_()
        self.folder = self.folder or pjoin(
            "$HOME",
            THREAD_LOCAL._pydal_folder_.split(os.sep + "applications" + os.sep, 1)[1],
        )
        ruri = self.uri.split("://", 1)[1]
        m = re.match(self.REGEX_URI, ruri)
        if not m:
            raise SyntaxError("Invalid URI string in DAL")
        self.driver_args["instance"] = self.credential_decoder(m.group("instance"))
        self.dbstring = self.credential_decoder(m.group("db"))
        self.createdb = self.adapter_args.get("createdb", True)
        if not self.createdb:
            self.driver_args["database"] = self.dbstring

    def find_driver(self):
        self.driver = "google"

    def connector(self):
        return rdbms.connect(**self.driver_args)

    def after_connection(self):
        if self.createdb:
            self.execute("CREATE DATABASE IF NOT EXISTS %s" % self.dbstring)
            self.execute("USE %s" % self.dbstring)
        self.execute("SET FOREIGN_KEY_CHECKS=1;")
        self.execute("SET sql_mode='NO_BACKSLASH_ESCAPES';")

    @with_connection_or_raise
    def execute(self, *args, **kwargs):
        command = self.filter_sql_command(args[0]).decode("utf8")
        handlers = self._build_handlers_for_execution()
        for handler in handlers:
            handler.before_execute(command)
        rv = self.cursor.execute(command, *args[1:], **kwargs)
        for handler in handlers:
            handler.after_execute(command)
        return rv


# based on this: https://cloud.google.com/appengine/docs/standard/python/cloud-sql/
@adapters.register_for("google:MySQLdb")
class GoogleMySQL(GoogleMigratorMixin, MySQL):
    uploads_in_blob = True
    drivers = ("MySQLdb",)

    def _find_work_folder(self):
        super(GoogleMySQL, self)._find_work_folder()
        if os.path.isabs(self.folder) and self.folder.startswith(os.getcwd()):
            self.folder = os.path.relpath(self.folder, os.getcwd())

    def after_connection(self):
        self.execute("SET FOREIGN_KEY_CHECKS=1;")
        self.execute("SET sql_mode='NO_BACKSLASH_ESCAPES,TRADITIONAL';")


@adapters.register_for("google:psycopg2")
class GooglePostgres(GoogleMigratorMixin, PostgrePsyco):
    uploads_in_blob = True
    drivers = ("psycopg2",)

    def _find_work_folder(self):
        super(GooglePostgres, self)._find_work_folder()
        if os.path.isabs(self.folder) and self.folder.startswith(os.getcwd()):
            self.folder = os.path.relpath(self.folder, os.getcwd())


@adapters.register_for("firestore")
class Firestore(NoSQLAdapter):
    dbengine = "firestore"

    REGEX_NAMESPACE = r".*//cred=(?P<cred>.+\.json)"

    def _initialize_(self):

        import firebase_admin
        from firebase_admin import credentials, firestore

        super(Firestore, self)._initialize_()
        match = re.match(self.REGEX_NAMESPACE, self.uri)
        if not match:
            # on google app engine
            cred = credentials.ApplicationDefault()
        else:
            # not on google app engine
            cred = credentials.Certificate(match.group("cred"))

        self._app = firebase_admin.initialize_app(cred)
        self._client = firestore.client()

    def find_driver(self):
        return

    def connector(self):
        return FakeDriver()

    def _expand(self, expression, field_type=None, query_env={}):
        if expression is None:
            return None
        elif isinstance(expression, Field):
            if expression.type in ("text", "blob", "json"):
                raise SyntaxError("AppEngine does not index by: %s" % expression.type)
            return expression.name
        elif isinstance(expression, (Expression, Query)):
            if expression.second is not None:
                return expression.op(
                    expression.first, expression.second, query_env=query_env
                )
            elif expression.first is not None:
                return expression.op(expression.first, query_env=query_env)
            else:
                return expression.op()
        elif field_type:
            return self.represent(expression, field_type)
        elif isinstance(expression, (list, tuple)):
            return ",".join([self.represent(item, field_type) for item in expression])
        elif hasattr(expression, "_FilterNode__name"):
            # check for _FilterNode__name to avoid explicit
            # import of FilterNode
            return expression
        else:
            raise NotImplementedError

    def represent(self, obj, field_type, tablename=None):
        if isinstance(obj, (Expression, Field)):
            raise SyntaxError("not supported on GAE")
        return super(Firestore, self).represent(obj, field_type)

    def apply_filter(self, source, table, query):

        from google.cloud.firestore_v1.base_query import FieldFilter

        if isinstance(query, Query) and query.first is table._id:
            if query.op.__name__ == "eq":
                return source.document(str(query.second)).get()
            elif query.op.__name__ == "gt" and query.second == 0:
                return source
            raise RuntimeError("operator not supported")

        filters = self.expand(query)
        if filters:
            if not isinstance(filters, list):
                filters = [filters]
            for filter in filters:
                source = source.where(filter=filter)
        return source

    def get_docs(self, table, query, orderby=None, limitby=None):

        from firebase_admin import firestore

        source = self._client.collection(table._tablename)
        source = self.apply_filter(source, table, query)

        if not hasattr(source, "where"):
            return [source]

        if limitby:
            if limitby[0]:
                raise SyntaxError("Firestore does not support an offset")
            source = source.limit(limitby[1])

        if orderby:
            if not isinstance(orderby, list):
                orderby = [orderby]
            for order in orderby:
                if isinstance(order, Field):
                    source = source.order_by(order.name)
                elif (
                    isinstance(order, Expression)
                    and order.op.__name__ == "invert"
                    and isinstance(order.first, Field)
                ):
                    source = source.order_by(
                        order.first.name, direction=firestore.Query.DESCENDING
                    )
                else:
                    raise RuntimeError(f"orderby {order} unsupported")

        return source.stream()

    def select(self, query, fields, attributes):
        attributes = attributes or {}

        # figure out what query and fields we want
        if query:
            table = self.get_table(query)
        elif fields:
            table = fields[0].table
            query = db._adapter.id_query(fields[0].table)
        else:
            raise SyntaxError("Unable to determine the table")

        if fields and not isinstance(fields, (list, tuple)):
            fields = [fields]
        if not fields or SQLALL in fields:
            fields = list(table)

        orderby = attributes.get("orderby")
        limitby = attributes.get("limitby")
        if attributes.get("distinct"):
            raise SyntaxError("Firestore does not support an distinct")
        docs = self.get_docs(table, query, orderby, limitby)

        # convert docs to rows
        rows = []
        for doc in docs:
            item = doc.to_dict()
            if not item:
                continue
            item["id"] = int(doc.id)
            rows.append([item.get(t.name) for t in fields])
        # postprocess the items
        colnames = [t.longname for t in fields]
        processor = attributes.get("processor", self.parse)
        return processor(rows, fields, colnames, False)

    def count(self, query, distinct=None, limit=None):
        # OK
        from google.cloud.firestore_v1 import aggregation

        if distinct:
            raise RuntimeError("COUNT DISTINCT not supported")
        table = self.get_table(query)
        source = self._client.collection(table._tablename)
        source = self.apply_filter(source, table, query)
        aggregate_query = aggregation.AggregationQuery(source)
        aggregate_query.count()
        results = aggregate_query.get()
        for result in results:
            return int(result[0].value)
        return 0

    def delete(self, table, query):
        counter = 0
        docs = list(self.get_docs(table, query))
        batch = self._client.batch()
        for doc in docs:
            batch.delete(doc.reference)
            counter += 1
        batch.commit()
        return counter

    def update(self, table, query, update_fields):
        counter = 0
        if any(f.name == "id" for f, v in update_fields):
            raise RuntimeError("Cannot update the id field")
        docs = list(self.get_docs(table, query))
        batch = self._client.batch()
        for doc in docs:
            batch.update(doc.reference, {f.name: v for f, v in update_fields})
            counter += 1
        batch.commit()
        return counter

    def truncate(self, table, mode=""):
        # OK
        def delete_collection(coll_ref, batch_size):
            if batch_size == 0:
                return
            batch = self._client.batch()
            docs = coll_ref.list_documents(page_size=batch_size)
            deleted = 0
            for doc in docs:
                batch.delete(doc)
                deleted = deleted + 1
            batch.commit()
            if deleted >= batch_size:
                return delete_collection(coll_ref, batch_size)

        delete_collection(self._client.collection(table._tablename), 1000)

    @staticmethod
    def make_id():
        return int(time.time() * int(1e21) + int.from_bytes(os.urandom(8)) % int(1e18))

    def insert(self, table, fields):
        # OK
        if any(f.name == "id" for f, v in fields):
            raise RuntimeError("Cannot update the id field")
        dfields = dict((f.name, self.represent(v, f.type)) for f, v in fields)
        id = self.make_id()
        collection = self._client.collection(table._tablename)
        doc = collection.document(str(id))
        doc.set(dfields)
        rid = Reference(id)
        rid._table, rid._record = table, None
        return rid

    def bulk_insert(self, table, items):
        # OK
        collection = self._client.collection(table._tablename)
        batch = self._client.batch()
        ids = []
        for fields in items:
            if any(f.name == "id" for f, v in fields):
                raise RuntimeError("Cannot update the id field")
            dfields = dict((f.name, self.represent(v, f.type)) for f, v in fields)
            id = self.make_id()
            doc = collection.document(str(id))
            batch.set(doc, dfields)
            rid = Reference(id)
            rid._table, rid._recor = table, None
            ids.append(rid)
        batch.commit()
        return ids
