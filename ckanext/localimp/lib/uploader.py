import os
import shutil
import cgi
import pylons
import datetime
import mimetypes
import pathlib2
import grp, pwd

import ckan.lib.munge as munge
import ckan.logic as logic
import ckan.model as model

import logging

config = pylons.config
log = logging.getLogger(__name__)

_storage_path = None
_max_resource_size = None
_max_image_size = None


def get_storage_path():
    '''Function to cache storage path'''
    global _storage_path

    # None means it has not been set. False means not in config.
    if _storage_path is None:
        storage_path = config.get('ckan.storage_path')
        ofs_impl = config.get('ofs.impl')
        ofs_storage_dir = config.get('ofs.storage_dir')
        if storage_path:
            _storage_path = storage_path
        elif ofs_impl == 'pairtree' and ofs_storage_dir:
            log.warn('''Please use config option ckan.storage_path instead of
                     ofs.storage_dir''')
            _storage_path = ofs_storage_dir
            return _storage_path
        elif ofs_impl:
            log.critical('''We only support local file storage form version 2.2
                         of ckan please specify ckan.storage_path in your
                         config for your uploads''')
            _storage_path = False
        else:
            log.critical('''Please specify a ckan.storage_path in your config
                         for your uploads''')
            _storage_path = False

    return _storage_path


def get_max_image_size():
    global _max_image_size
    if _max_image_size is None:
        _max_image_size = int(config.get('ckan.max_image_size', 2))
    return _max_image_size


def get_max_resource_size():
    global _max_resource_size
    if _max_resource_size is None:
        _max_resource_size = int(config.get('ckan.max_resource_size', 10))
    return _max_resource_size


class LocalimpUpload(object):
    def __init__(self, object_type, old_filename=None):
        ''' Setup upload by creating a subdirectory of the storage directory
        of name object_type. old_filename is the name of the file in the url
        field last time'''

        self.storage_path = None
        self.filename = None
        self.filepath = None
        path = get_storage_path()
        if not path:
            return
        self.storage_path = os.path.join(path, 'storage',
                                         'uploads', object_type)
        try:
            os.makedirs(self.storage_path)
        except OSError, e:
            # errno 17 is file already exists
            if e.errno != 17:
                raise
        self.object_type = object_type
        self.old_filename = old_filename
        if old_filename:
            self.old_filepath = os.path.join(self.storage_path, old_filename)

    def update_data_dict(self, data_dict, url_field, file_field, clear_field):
        ''' Manipulate data from the data_dict.  url_field is the name of the
        field where the upload is going to be. file_field is name of the key
        where the FieldStorage is kept (i.e the field where the file data
        actually is). clear_field is the name of a boolean field which
        requests the upload to be deleted.  This needs to be called before
        it reaches any validators'''

        self.url = data_dict.get(url_field, '')
        self.clear = data_dict.pop(clear_field, None)
        self.file_field = file_field
        self.upload_field_storage = data_dict.pop(file_field, None)

        if not self.storage_path:
            return

        if isinstance(self.upload_field_storage, cgi.FieldStorage):
            self.filename = self.upload_field_storage.filename
            self.filename = str(datetime.datetime.utcnow()) + self.filename
            self.filename = munge.munge_filename_legacy(self.filename)
            self.filepath = os.path.join(self.storage_path, self.filename)
            data_dict[url_field] = self.filename
            self.upload_file = self.upload_field_storage.file
            self.tmp_filepath = self.filepath + '~'
        # keep the file if there has been no change
        elif self.old_filename and not self.old_filename.startswith('http'):
            if not self.clear:
                data_dict[url_field] = self.old_filename
            if self.clear and self.url == self.old_filename:
                data_dict[url_field] = ''

    def upload(self, max_size=2):
        ''' Actually upload the file.
        This should happen just before a commit but after the data has
        been validated and flushed to the db. This is so w
        anything unless the request is actually good.
        max_size is size in MB maximum of the file'''

        if self.filename:
            output_file = open(self.tmp_filepath, 'wb')
            self.upload_file.seek(0)
            current_size = 0
            while True:
                current_size = current_size + 1
                # MB chunks
                data = self.upload_file.read(2 ** 20)
                if not data:
                    break
                output_file.write(data)
                if current_size > max_size:
                    os.remove(self.tmp_filepath)
                    raise logic.ValidationError(
                        {self.file_field: ['File upload too large']}
                    )
            output_file.close()
            os.rename(self.tmp_filepath, self.filepath)
            self.clear = True

        if (self.clear and self.old_filename
                and not self.old_filename.startswith('http')):
            try:
                os.remove(self.old_filepath)
            except OSError:
                pass

class LocalimpResourceUpload(object):
    def __init__(self, resource):
        path = get_storage_path()
        if not path:
            self.storage_path = None
            return
        self.storage_path = os.path.join(path, 'resources')
        try:
            os.makedirs(self.storage_path)
        except OSError, e:
            # errno 17 is file already exists
            if e.errno != 17:
                raise
        self.filename = None

        url = resource.get('url')
        upload_field = resource.pop('upload', None)
        self.clear = resource.pop('clear_upload', None)

        # upload is FieldStorage
        if isinstance(upload_field, cgi.FieldStorage):
            self.upload_type = 'remote'
            self.filename = upload_field.filename
            # GAS 2018-03-01 do not remove underscores
            # self.filename = munge.munge_filename(self.filename)
            #resource['url'] = munge.munge_filename(self.filename)
            resource['url'] = _munge_filename(self.filename)
            resource['url_type'] = 'upload'
            resource['last_modified'] = datetime.datetime.utcnow()
            self.url_type = resource['url_type']
            self.upload_file = upload_field.file
        # upload is path to local file
        elif isinstance(upload_field, pathlib2.Path):
            self.upload_type = 'local'
            self.localpath = upload_field.absolute()
            self.filename = upload_field.name
            # GAS 2018-03-01 do not remove underscores
            # self.filename = munge.munge_filename(self.filename)
            #resource['url'] = munge.munge_filename(self.filename)
            resource['url'] = _munge_filename(self.filename)
            resource['url_type'] = 'upload'
            resource['last_modified'] = datetime.datetime.utcnow()
            self.url_type = resource['url_type']
        elif self.clear:
            resource['url_type'] = ''

    def get_directory(self, id):
        directory = os.path.join(self.storage_path,
                                 id[0:3], id[3:6])
        return directory

    def get_path(self, id):
        directory = self.get_directory(id)
        filepath = os.path.join(directory, id[6:])
        return filepath

    def upload(self, id, max_size=10):
        '''Actually upload the file.

        :returns: ``'file uploaded'`` if a new file was successfully uploaded
            (whether it overwrote a previously uploaded file or not),
            ``'file deleted'`` if an existing uploaded file was deleted,
            or ``None`` if nothing changed
        :rtype: ``string`` or ``None``

        '''
        if not self.storage_path:
            return

        # Get directory and filepath on the system
        # where the file for this resource will be stored
        directory = self.get_directory(id)
        filepath = self.get_path(id)

        # If a filename has been provided (a file is being uploaded)
        # we write it to the filepath (and overwrite it if it already
        # exists). This way the uploaded file will always be stored
        # in the same location
        if self.filename:
            try:
                os.makedirs(directory)
            except OSError, e:
                # errno 17 is file already exists
                if e.errno != 17:
                    raise
            if self.upload_type == 'remote':
                tmp_filepath = filepath + '~'
                output_file = open(tmp_filepath, 'wb+')
                self.upload_file.seek(0)
                current_size = 0
                while True:
                    current_size = current_size + 1
                    # MB chunks
                    data = self.upload_file.read(2 ** 20)
                    if not data:
                        break
                    output_file.write(data)
                    if current_size > max_size:
                        os.remove(tmp_filepath)
                        raise logic.ValidationError(
                            {'upload': ['File upload too large']}
                        )
                output_file.close()
                os.rename(tmp_filepath, filepath)
                return
            elif self.upload_type == 'local':
                # Move file to ckan file system path
                # os.rename(os.path.join(self.filepath,self.filename),filepath)
                shutil.move(str(self.localpath),filepath)
                uid = pwd.getpwnam("ckan").pw_uid
                gid = grp.getgrnam("www-data").gr_gid
                os.chown(filepath, uid, gid)
                os.chmod(filepath, 0640)
                return

        # The resource form only sets self.clear (via the input clear_upload)
        # to True when an uploaded file is not replaced by another uploaded
        # file, only if it is replaced by a link to file.
        # If the uploaded file is replaced by a link, we should remove the
        # previously uploaded file to clean up the file system.
        if self.clear:
            try:
                os.remove(filepath)
            except OSError, e:
                pass

def _munge_filename(filename):
    ''' Tidies a filename

    Keeps the filename extension (e.g. .csv).
    Strips off any path on the front.
    '''
    import re
    import os.path

    # just get the filename ignore the path
    path, filename = os.path.split(filename)
    # clean up
    filename = munge.substitute_ascii_equivalents(filename)
    filename = filename.lower().strip()
    filename = re.sub(r'[^a-zA-Z0-9_. -]', '', filename).replace(' ', '-')
    # resize if needed but keep extension
    name, ext = os.path.splitext(filename)
    # limit overly long extensions
    if len(ext) > 21:
        ext = ext[:21]
    # max/min size
    ext_length = len(ext)
    name = _munge_to_length(name, max(3 - ext_length, 1), 100 - ext_length)
    filename = name + ext

    return filename

def _munge_to_length(string, min_length, max_length):
    '''Pad/truncates a string'''
    if len(string) < min_length:
        string += '_' * (min_length - len(string))
    if len(string) > max_length:
        string = string[:max_length]
    return string
