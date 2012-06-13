#!/usr/bin/env python

import copy
import glob
import logging
import os
import re
import simplejson as json
import sqlite3
import sys
import time
import types
import urllib

import memcacheConstants

from pump import Source, Sink, Batch, SinkBatchFuture

CBB_VERSION = 2001 # sqlite pragma user version.

# TODO: (2) BFD - split *.cbb files when they get too big.

class BFD:
    """Mixin for backup-file/directory EndPoint helper methods."""

    def bucket_name(self):
        return self.source_bucket['name']

    def node_name(self):
        return self.source_node['hostname']

    @staticmethod
    def design_path(spec, bucket_name):
        return os.path.normpath(spec) + \
            '/bucket-' + urllib.quote_plus(bucket_name) + \
            '/design.json'

    @staticmethod
    def db_dir(spec, bucket_name, node_name):
        return os.path.normpath(spec) + \
            '/bucket-' + urllib.quote_plus(bucket_name) + \
            '/node-' + urllib.quote_plus(node_name)

    @staticmethod
    def db_path(spec, bucket_name, node_name):
        return BFD.db_dir(spec, bucket_name, node_name) + "/data-0000.cbb"


# --------------------------------------------------

class BFDSource(BFD, Source):
    """Can read from backup-file/directory layout."""

    def __init__(self, opts, spec, source_bucket, source_node,
                 source_map, sink_map, ctl, cur):
        Source.__init__(self, opts, spec, source_bucket, source_node,
                 source_map, sink_map, ctl, cur)

        self.cursor_db = None
        self.cursor_done = False

    @staticmethod
    def can_handle(opts, spec):
        # TODO: (1) BFDSource - better can_handle() checking.
        return os.path.isdir(spec)

    @staticmethod
    def check(opts, spec):
        spec = os.path.normpath(spec)
        if not os.path.isdir(spec):
            return "error: backup_dir is not a directory: " + spec, None

        buckets = []

        bucket_dirs = glob.glob(spec + "/bucket-*")
        for bucket_dir in sorted(bucket_dirs):
            if not os.path.isdir(bucket_dir):
                return "error: not a bucket directory: " + bucket_dir, None

            bucket_name = os.path.basename(bucket_dir)[len("bucket-"):].strip()
            bucket_name = urllib.unquote_plus(bucket_name)
            if not bucket_name:
                return "error: bucket_name too short: " + bucket_dir, None

            bucket = { 'name': bucket_name, 'nodes': [] }
            buckets.append(bucket)

            node_dirs = glob.glob(bucket_dir + "/node-*")
            for node_dir in sorted(node_dirs):
                if not os.path.isdir(node_dir):
                    return "error: not a node directory: " + node_dir, None

                node_name = os.path.basename(node_dir)[len("node-"):].strip()
                node_name = urllib.unquote_plus(node_name)
                if not node_name:
                    return "error: node_name too short: " + node_dir, None

                bucket['nodes'].append({ 'hostname': node_name })

        return 0, { 'spec': spec,
                    'buckets': buckets }

    @staticmethod
    def provide_config(opts, source_spec, source_bucket, source_map):
        # TODO: (3) BFDSource - provide_config implementation.
        return 0, None

    @staticmethod
    def provide_design(opts, source_spec, source_bucket, source_map):
        fname = BFD.design_path(source_spec, source_bucket['name'])
        if os.path.exists(fname):
            try:
                f = open(fname, 'r')
                d = f.read()
                f.close()
                return 0, d
            except IOError as e:
                return ("error: could not read design: %s" +
                        "; exception: %s") % (fname, e), None
        return 0, None

    def provide_batch(self):
        if self.cursor_done:
            return 0, None

        batch = Batch(self)

        batch_max_size = self.opts.extra['batch_max_size']
        batch_max_bytes = self.opts.extra['batch_max_bytes']

        s = "SELECT cmd, vbucket_id, key, flg, exp, cas, val FROM cbb_cmd"

        try:
            if self.cursor_db is None:
                rv, db = self.connect_db()
                if rv != 0:
                    return rv, None

                cursor = db.cursor()
                cursor.execute(s)

                self.cursor_db = (cursor, db)

            cursor, db = self.cursor_db

            while (not self.cursor_done and
                   batch.size() < batch_max_size and
                   batch.bytes < batch_max_bytes):
                row = cursor.fetchone()
                if row:
                    vbucket_id = row[1]
                    key = row[2]
                    val = row[6]

                    if self.skip(key, vbucket_id):
                        continue

                    batch.append(row, len(val))
                else:
                    self.cursor_done = True
                    if self.cursor_db:
                        self.cursor_db[0].close()
                        self.cursor_db[1].close()
                    self.cursor_db = None

        except Exception:
            self.cursor_done = True
            if self.cursor_db:
                self.cursor_db[0].close()
                self.cursor_db[1].close()
            self.cursor_db = None

        return 0, batch

    def connect_db(self):
        return connect_db(BFD.db_path(self.spec,
                                      self.bucket_name(),
                                      self.node_name()),
                          self.opts, CBB_VERSION)

    @staticmethod
    def total_items(opts, source_bucket, source_node, source_map):
        rv, db = connect_db(BFD.db_path(source_map['spec'],
                                        source_bucket['name'],
                                        source_node['hostname']),
                            opts, CBB_VERSION)
        if rv != 0:
            return rv, None

        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM cbb_cmd;")
        tot = cur.fetchone()[0]
        cur.close()
        db.close()

        return 0, tot


# --------------------------------------------------

class BFDSink(BFD, Sink):
    """Can write to backup-file/directory layout."""

    def __init__(self, opts, spec, source_bucket, source_node,
                 source_map, sink_map, ctl, cur):
        Sink.__init__(self, opts, spec, source_bucket, source_node,
                 source_map, sink_map, ctl, cur)

        self.init_worker(BFDSink.run)

    @staticmethod
    def run(self):
        """Worker thread to asynchronously store incoming batches into db."""

        s = "INSERT INTO cbb_cmd (cmd, vbucket_id, key, flg, exp, cas, val)" \
            " VALUES (?, ?, ?, ?, ?, ?, ?)"

        db = None

        while not self.ctl['stop']:
            batch, future = self.pull_next_batch()
            if not batch:
                return self.future_done(future, 0)

            if not db:
                rv, db = self.create_db()
                if rv != 0:
                    return self.future_done(future, rv)

            try:
                c = db.cursor()

                for i in range(0, batch.size()):
                    cmd, vbucket_id, key, flg, exp, cas, val = \
                        batch.item(i)

                    if self.skip(key, vbucket_id):
                        continue

                    if (cmd != memcacheConstants.CMD_TAP_MUTATION and
                        cmd != memcacheConstants.CMD_TAP_DELETE):
                        return self.future_done(future,
                                                "error: BFDSink bad cmd: " +
                                                str(cmd))

                    c.execute(s, (cmd, vbucket_id,
                                  sqlite3.Binary(key),
                                  flg, exp, cas,
                                  sqlite3.Binary(val)))

                db.commit()

                self.future_done(future, 0) # No return to keep looping.

            except sqlite3.Error as e:
                return self.future_done(future, "error: db error: " + str(e))
            except Exception as e:
                return self.future_done(future, "error: db exception: " + str(e))

    @staticmethod
    def can_handle(opts, spec):
        spec = os.path.normpath(spec)

        # TODO: (1) BFDSink - can_handle() needs tighter implementation.

        return ((os.path.isdir(spec) == True) or
                (os.path.exists(spec) == False and
                 os.path.isdir(os.path.dirname(spec)) == True))

    @staticmethod
    def check(opts, spec, source_map):
        # TODO: (2) BFDSink - check disk space.
        # TODO: (2) BFDSink - check should report on pre-creatable directories.

        spec = os.path.normpath(spec)

        # Check that directory's empty.
        if os.path.exists(spec):
            if not os.path.isdir(spec):
                return "error: backup directory is not a directory: " + spec, None
            if not os.access(spec, os.W_OK):
                return "error: backup directory is not writable: " + spec, None
            if len(os.listdir(spec)) != 0:
                return "error: backup directory is not empty: " + spec, None
            return 0, None

        # Or, that the parent directory exists.
        parent_dir = os.path.dirname(spec)

        if not os.path.exists(parent_dir):
            return "error: missing parent directory: " + parent_dir, None
        if not os.path.isdir(parent_dir):
            return "error: parent directory is not a directory: " + parent_dir, None
        if not os.access(parent_dir, os.W_OK):
            return "error: parent directory is not writable: " + parent_dir, None
        return 0, None

    @staticmethod
    def consume_config(opts, sink_spec, sink_map,
                       source_bucket, source_map, source_config):
        # TODO: (4) BFDSink - consume_config.
        return 0

    @staticmethod
    def consume_design(opts, sink_spec, sink_map,
                       source_bucket, source_map, source_design):
        if source_design:
            fname = BFD.design_path(sink_spec,
                                    source_bucket['name'])
            try:
                f = open(fname, 'w')
                f.write(source_design)
                f.close()
            except IOError as e:
                return ("error: could not write design: %s" +
                        "; exception: %s") % (fname, e), None
        return 0

    def consume_batch_async(self, batch):
        return self.push_next_batch(batch, SinkBatchFuture(self, batch))

    def create_db(self):
        rv = self.mkdirs()
        if rv != 0:
            return rv, None

        rv, db = create_db(BFD.db_path(self.spec,
                                       self.bucket_name(),
                                       self.node_name()),
                           self.opts)
        if rv != 0:
            return rv, None

        try:
            cur = db.cursor()
            cur.execute("INSERT INTO cbb_meta (key, val) VALUES (?, ?)",
                        ("source_bucket.json",
                         json.dumps(cleanse(self.source_bucket))))
            cur.execute("INSERT INTO cbb_meta (key, val) VALUES (?, ?)",
                        ("source_node.json",
                         json.dumps(cleanse(self.source_node))))
            cur.execute("INSERT INTO cbb_meta (key, val) VALUES (?, ?)",
                        ("source_map.json",
                         json.dumps(cleanse(self.source_map))))
            cur.execute("INSERT INTO cbb_meta (key, val) VALUES (?, ?)",
                        ("start.datetime", time.strftime("%Y/%m/%d-%H:%M:%S")))
            db.commit()
        except sqlite3.Error as e:
            return "error: create_db error: " + str(e), None
        except Exception as e:
            return "error: create_db exception: " + str(e), None

        return 0, db

    def mkdirs(self):
        """Make directories, if not already, with structure like...
           <spec>/
             bucket-<BUCKETNAME>/
               design.json
               node-<NODE>/
                 data-<XXXX>.cbb"""
        spec = os.path.normpath(self.spec)
        if not os.path.isdir(spec):
            try:
                os.mkdir(spec)
            except OSError as e:
                return "error: could not mkdir: %s; exception: %s" % (spec, e)

        d = BFD.db_dir(self.spec, self.bucket_name(), self.node_name())
        if not os.path.isdir(d):
            try:
                os.makedirs(d)
            except OSError as e:
                return "error: could not mkdirs: %s; exception: %s" % (d, e)
        return 0


# --------------------------------------------------

def create_db(db_path, opts):
    try:
        logging.debug("  create_db: " + db_path)

        rv, db = connect_db(db_path, opts, 0)
        if rv != 0:
            return rv, None

        db.executescript("""
                  BEGIN;
                  CREATE TABLE cbb_cmd
                     (cmd integer,
                      vbucket_id integer,
                      key blob, flg integer, exp integer, cas integer,
                      val blob);
                  CREATE TABLE cbb_meta
                     (key text,
                      val blob);
                  pragma user_version=%s;
                  COMMIT;
                """ % (CBB_VERSION))

        return 0, db

    except Exception as e:
        return "error: create_db exception: " + str(e), None

def connect_db(db_path, opts, version):
    try:
        # TODO: (1) BFD - connect_db - use pragma page_size.
        # TODO: (1) BFD - connect_db - use pragma max_page_count.
        # TODO: (1) BFD - connect_db - use pragma journal_mode.

        logging.debug("  connect_db: " + db_path)

        db = sqlite3.connect(db_path)
        db.text_factory = str

        cur = db.execute("pragma user_version").fetchall()[0][0]
        if cur != version:
            return "error: unexpected db user version: " + \
                str(cur) + " vs " + str(version), \
                None

        return 0, db

    except Exception as e:
        return "error: connect_db exception: " + str(e), None

def cleanse(d):
    """Elide passwords from hierarchy of dict/list's."""
    return cleanse_helper(copy.deepcopy(d))

def cleanse_helper(d):
    """Recursively, destructively elide passwords from hierarchy of dict/list's."""
    if type(d) == types.ListType:
        for x in d:
            cleanse_helper(x)
    elif type(d) == types.DictType:
        for k, v in d.iteritems():
            if "assword" in k:
                d[k] = '<...ELIDED...>'
            else:
                d[k] = cleanse_helper(v)
    return d