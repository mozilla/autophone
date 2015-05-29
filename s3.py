# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# modeled after https://github.com/mozilla-b2g/gaia/blob/master/tests/python/gaia-ui-tests/gaiatest/mixins/treeherder.py

import gzip
import logging
import os
import re
import tempfile

import boto
import boto.s3.connection

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

class S3Error(Exception):
    def __init__(self, message):
        Exception.__init__(self, 'S3Error: %s' % message)

class S3Bucket(object):

    def __init__(self, bucket_name, access_key_id, access_secret_key):
        self.bucket_name = bucket_name
        self._bucket = None
        self.access_key_id = access_key_id
        self.access_secret_key = access_secret_key

    @property
    def bucket(self):
        if self._bucket:
            return self._bucket
        try:
            conn = boto.s3.connection.S3Connection(self.access_key_id,
                                                   self.access_secret_key)
            if not conn.lookup(self.bucket_name):
                raise S3Error('bucket %s not found' % self.bucket_name)
            if not self._bucket:
                self._bucket = conn.get_bucket(self.bucket_name)
            return self._bucket
        except boto.exception.NoAuthHandlerFound:
            logger.exception()
            raise S3Error('Authentication failed')
        except boto.exception.S3ResponseError, e:
            logger.exception()
            raise S3Error('%s' % e)

    def ls(self, keypattern='.*', prefix=''):
        if isinstance(keypattern, str):
            keypattern = re.compile(keypattern)
        keys = [key for key in self.bucket.list(prefix=prefix) if keypattern.match(key.name)]
        return keys

    def rm(self, keys=[], keypattern=None, prefix=''):
        assert isinstance(keys, list) or isinstance(keypattern, str)

        if isinstance(keypattern, str):
            keys = self.ls(prefix=prefix, keypattern=keypattern)
        try:
            for key in keys:
                key.delete()
        except boto.exception.S3ResponseError, e:
            logger.exception(str(e))
            raise S3Error('%s' % e)

    def upload(self, path, destination):
        try:
            key = self.bucket.get_key(destination)
            if not key:
                logger.debug('Creating key: %s' % destination)
                key = self.bucket.new_key(destination)

            ext = os.path.splitext(path)[-1]
            if ext == '.log' or ext == '.txt':
                key.set_metadata('Content-Type', 'text/plain')

            with tempfile.NamedTemporaryFile('w+b', suffix=ext) as tf:
                logger.debug('Compressing: %s' % path)
                with gzip.GzipFile(path, 'wb', fileobj=tf) as gz:
                    with open(path, 'rb') as f:
                        gz.writelines(f)
                tf.flush()
                tf.seek(0)
                key.set_metadata('Content-Encoding', 'gzip')
                logger.debug('Setting key contents from: %s' % tf.name)
                key.set_contents_from_file(tf)

            url = key.generate_url(expires_in=0,
                                   query_auth=False)
        except boto.exception.S3ResponseError, e:
            logger.exception(str(e))
            raise S3Error('%s' % e)

        logger.debug('File %s uploaded to: %s' % (path, url))
        return url

if __name__ == '__main__':
    import ConfigParser
    import sys
    from optparse import OptionParser

    logging.basicConfig()
    logger.setLevel(logging.INFO)

    parser = OptionParser()
    parser.set_usage("""
    usage: %prog [options]

    --s3-upload-bucket, --aws-access-key-id, --aws-access-key must be specified
    together either as command line options or in the --config file.

    --upload and --key must be specified together.
    """)
    parser.add_option('--s3-upload-bucket',
                      dest='s3_upload_bucket',
                      action='store',
                      type='string',
                      default=None,
                      help="""AWS S3 bucket name used to store logs.
                      Defaults to None. If specified, --aws-access-key-id
                      and --aws-secret-access-key must also be specified.
                      """)
    parser.add_option('--aws-access-key-id',
                      dest='aws_access_key_id',
                      action='store',
                      type='string',
                      default=None,
                      help="""AWS Access Key ID used to access AWS S3.
                      Defaults to None. If specified, --s3-upload-bucket
                      and --aws-secret-access-key must also be specified.
                      """)
    parser.add_option('--aws-access-key',
                      dest='aws_access_key',
                      action='store',
                      type='string',
                      default=None,
                      help="""AWS Access Key used to access AWS S3.
                      Defaults to None. If specified, --s3-upload-bucket
                      and --aws-secret-access-key-id must also be specified.
                      """)
    parser.add_option('--config',
                      dest='s3config',
                      action='store',
                      type='string',
                      default=None,
                      help="""Optional s3.py configuration ini file.
                      Defaults to None. If specified, must specify
                      s3_upload_bucket, aws_access_key_id, and
                      aws_access_key options.
                      """)
    parser.add_option('--ls',
                      dest='ls',
                      action='store',
                      type='string',
                      default=None,
                      help='List matching keys in bucket.')
    parser.add_option('--rm',
                      dest='rm',
                      action='store',
                      type='string',
                      default=None,
                      help='Delete matching keys in bucket.')
    parser.add_option('--prefix',
                      dest='prefix',
                      action='store',
                      type='string',
                      default='',
                      help='Limit matching keys by prefix.')
    parser.add_option('--upload',
                      dest='upload',
                      action='store',
                      type='string',
                      default=None,
                      help="""File to upload file to bucket.
                      If --upload is specified, --key must also
                      be specified.""")
    parser.add_option('--key',
                      dest='key',
                      action='store',
                      type='string',
                      default=None,
                      help="""Bucket key for uploaded file.
                      If --upload is specified, --key must also
                      be specified.""")

    (cmd_options, args) = parser.parse_args()

    if cmd_options.s3config:
        cfg = ConfigParser.RawConfigParser()
        cfg.read(cmd_options.s3config)

        cmd_options.s3_upload_bucket = cfg.get('settings', 's3_upload_bucket')
        cmd_options.aws_access_key_id = cfg.get('settings', 'aws_access_key_id')
        cmd_options.aws_access_key = cfg.get('settings', 'aws_access_key')

    if (not cmd_options.s3_upload_bucket or
        not cmd_options.aws_access_key_id or
        not cmd_options.aws_access_key):

        parser.error('--s3-upload-bucket, --aws-access-key-id, --aws-access-key must be specified '
                     'together either as command line options or in the --config file.')
        parser.print_usage()
        sys.exit(1)

    if ((cmd_options.upload or cmd_options.key) and (
            not cmd_options.upload or not cmd_options.key)):
        parser.error('--upload and --key must be specified together.')
        parser.print_usage()
        sys.exit(1)

    if (not cmd_options.s3_upload_bucket and
        not cmd_options.aws_access_key_id and
        not cmd_options.aws_access_key and
        not cmd_options.config and
        not cmd_options.ls and
        not cmd_options.rm and
        not cmd_options.upload and
        not cmd_options.key):
        parser.print_usage()
        sys.exit(1)

    logger.debug('bucket %s' % cmd_options.s3_upload_bucket)

    s3bucket = S3Bucket(cmd_options.s3_upload_bucket,
                        cmd_options.aws_access_key_id,
                        cmd_options.aws_access_key)

    if cmd_options.upload:
        print s3bucket.upload(cmd_options.upload, cmd_options.key)
    if cmd_options.ls:
        for key in s3bucket.ls(prefix=cmd_options.prefix,
                               keypattern=cmd_options.ls):
            print key.name
    if cmd_options.rm:
        s3bucket.rm(prefix=cmd_options.prefix,
                    keypattern=cmd_options.rm)
