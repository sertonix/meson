# Copyright 2015 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .. import mlog
import contextlib
import urllib.request, os, hashlib, shutil, tempfile, stat
import subprocess
import sys
import configparser
from pathlib import Path
from . import WrapMode
from ..mesonlib import Popen_safe

try:
    import ssl
    has_ssl = True
    API_ROOT = 'https://wrapdb.mesonbuild.com/v1/'
except ImportError:
    has_ssl = False
    API_ROOT = 'http://wrapdb.mesonbuild.com/v1/'

req_timeout = 600.0
ssl_warning_printed = False

def build_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ctx.options |= ssl.OP_NO_SSLv2
    ctx.options |= ssl.OP_NO_SSLv3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_default_certs()
    return ctx

def quiet_git(cmd, workingdir):
    pc = subprocess.Popen(['git', '-C', workingdir] + cmd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = pc.communicate()
    if pc.returncode != 0:
        return False, err
    return True, out

def open_wrapdburl(urlstring):
    global ssl_warning_printed
    if has_ssl:
        try:
            return urllib.request.urlopen(urlstring, timeout=req_timeout)# , context=build_ssl_context())
        except urllib.error.URLError:
            if not ssl_warning_printed:
                print('SSL connection failed. Falling back to unencrypted connections.')
                ssl_warning_printed = True
    if not ssl_warning_printed:
        print('Warning: SSL not available, traffic not authenticated.',
              file=sys.stderr)
        ssl_warning_printed = True
    # Trying to open SSL connection to wrapdb fails because the
    # certificate is not known.
    if urlstring.startswith('https'):
        urlstring = 'http' + urlstring[5:]
    return urllib.request.urlopen(urlstring, timeout=req_timeout)


class PackageDefinition:
    def __init__(self, fname):
        self.config = configparser.ConfigParser()
        self.config.read(fname)
        self.wrap_section = self.config.sections()[0]
        if not self.wrap_section.startswith('wrap-'):
            raise RuntimeError('Invalid format of package file')
        self.type = self.wrap_section[5:]
        self.values = dict(self.config[self.wrap_section])

    def get(self, key):
        return self.values[key]

    def has_patch(self):
        return 'patch_url' in self.values

class Resolver:
    def __init__(self, subdir_root, wrap_mode=WrapMode.default):
        self.wrap_mode = wrap_mode
        self.subdir_root = subdir_root
        self.cachedir = os.path.join(self.subdir_root, 'packagecache')

    def resolve(self, packagename):
        self.packagename = packagename
        # We always have to load the wrap file, if it exists, because it could
        # override the default directory name.
        p = self.load_wrap()
        directory = packagename
        if p and 'directory' in p.values:
            directory = p.get('directory')
        dirname = os.path.join(self.subdir_root, directory)
        subprojdir = os.path.join(*Path(dirname).parts[-2:])
        meson_file = os.path.join(dirname, 'meson.build')

        # The directory is there and has meson.build? Great, use it.
        if os.path.exists(meson_file):
            return directory

        # Check if the subproject is a git submodule
        self.resolve_git_submodule(dirname)

        if os.path.exists(dirname):
            if not os.path.isdir(dirname):
                m = '{!r} already exists and is not a dir; cannot use as subproject'
                raise RuntimeError(m.format(subprojdir))
        else:
            # A wrap file is required to download
            if not p:
                m = 'No {}.wrap found for {!r}'
                raise RuntimeError(m.format(packagename, subprojdir))

            if p.type == 'file':
                self.get_file(p)
            else:
                self.check_can_download()
                if p.type == 'git':
                    self.get_git(p)
                elif p.type == "hg":
                    self.get_hg(p)
                elif p.type == "svn":
                    self.get_svn(p)
                else:
                    raise AssertionError('Unreachable code.')

        # A meson.build file is required in the directory
        if not os.path.exists(meson_file):
            m = '{!r} is not empty and has no meson.build files'
            raise RuntimeError(m.format(subprojdir))

        return directory

    def load_wrap(self):
        fname = os.path.join(self.subdir_root, self.packagename + '.wrap')
        if os.path.isfile(fname):
            return PackageDefinition(fname)
        return None

    def check_can_download(self):
        # Don't download subproject data based on wrap file if requested.
        # Git submodules are ok (see above)!
        if self.wrap_mode is WrapMode.nodownload:
            m = 'Automatic wrap-based subproject downloading is disabled'
            raise RuntimeError(m)

    def resolve_git_submodule(self, dirname):
        # Are we in a git repository?
        ret, out = quiet_git(['rev-parse'], self.subdir_root)
        if not ret:
            return False
        # Is `dirname` a submodule?
        ret, out = quiet_git(['submodule', 'status', dirname], self.subdir_root)
        if not ret:
            return False
        # Submodule has not been added, add it
        if out.startswith(b'+'):
            mlog.warning('git submodule {} might be out of date'.format(dirname))
            return True
        elif out.startswith(b'U'):
            raise RuntimeError('submodule {} has merge conflicts'.format(dirname))
        # Submodule exists, but is deinitialized or wasn't initialized
        elif out.startswith(b'-'):
            if subprocess.call(['git', '-C', self.subdir_root, 'submodule', 'update', '--init', dirname]) == 0:
                return True
            raise RuntimeError('Failed to git submodule init {!r}'.format(dirname))
        # Submodule looks fine, but maybe it wasn't populated properly. Do a checkout.
        elif out.startswith(b' '):
            subprocess.call(['git', 'checkout', '.'], cwd=dirname)
            # Even if checkout failed, try building it anyway and let the user
            # handle any problems manually.
            return True
        elif out == b'':
            # It is not a submodule, just a folder that exists in the main repository.
            return False
        m = 'Unknown git submodule output: {!r}'
        raise RuntimeError(m.format(out))

    def get_file(self, p):
        path = self.get_file_internal(p, 'source')
        target_dir = os.path.join(self.subdir_root, p.get('directory'))
        extract_dir = self.subdir_root
        # Some upstreams ship packages that do not have a leading directory.
        # Create one for them.
        try:
            p.get('lead_directory_missing')
            os.mkdir(target_dir)
            extract_dir = target_dir
        except KeyError:
            pass
        shutil.unpack_archive(path, extract_dir)
        if p.has_patch():
            self.apply_patch(p)

    def get_git(self, p):
        checkoutdir = os.path.join(self.subdir_root, p.get('directory'))
        revno = p.get('revision')
        is_there = os.path.isdir(checkoutdir)
        if is_there:
            try:
                subprocess.check_call(['git', 'rev-parse'], cwd=checkoutdir)
            except subprocess.CalledProcessError:
                raise RuntimeError('%s is not empty but is not a valid '
                                   'git repository, we can not work with it'
                                   ' as a subproject directory.' % (
                                       checkoutdir))

            if revno.lower() == 'head':
                # Failure to do pull is not a fatal error,
                # because otherwise you can't develop without
                # a working net connection.
                subprocess.call(['git', 'pull'], cwd=checkoutdir)
            else:
                if subprocess.call(['git', 'checkout', revno], cwd=checkoutdir) != 0:
                    subprocess.check_call(['git', 'fetch', p.get('url'), revno], cwd=checkoutdir)
                    subprocess.check_call(['git', 'checkout', revno],
                                          cwd=checkoutdir)
        else:
            if p.values.get('clone-recursive', '').lower() == 'true':
                subprocess.check_call(['git', 'clone', '--recursive', p.get('url'),
                                       p.get('directory')], cwd=self.subdir_root)
            else:
                subprocess.check_call(['git', 'clone', p.get('url'),
                                       p.get('directory')], cwd=self.subdir_root)
            if revno.lower() != 'head':
                if subprocess.call(['git', 'checkout', revno], cwd=checkoutdir) != 0:
                    subprocess.check_call(['git', 'fetch', p.get('url'), revno], cwd=checkoutdir)
                    subprocess.check_call(['git', 'checkout', revno],
                                          cwd=checkoutdir)
            push_url = p.values.get('push-url')
            if push_url:
                subprocess.check_call(['git', 'remote', 'set-url',
                                       '--push', 'origin', push_url],
                                      cwd=checkoutdir)

    def get_hg(self, p):
        checkoutdir = os.path.join(self.subdir_root, p.get('directory'))
        revno = p.get('revision')
        is_there = os.path.isdir(checkoutdir)
        if is_there:
            if revno.lower() == 'tip':
                # Failure to do pull is not a fatal error,
                # because otherwise you can't develop without
                # a working net connection.
                subprocess.call(['hg', 'pull'], cwd=checkoutdir)
            else:
                if subprocess.call(['hg', 'checkout', revno], cwd=checkoutdir) != 0:
                    subprocess.check_call(['hg', 'pull'], cwd=checkoutdir)
                    subprocess.check_call(['hg', 'checkout', revno],
                                          cwd=checkoutdir)
        else:
            subprocess.check_call(['hg', 'clone', p.get('url'),
                                   p.get('directory')], cwd=self.subdir_root)
            if revno.lower() != 'tip':
                subprocess.check_call(['hg', 'checkout', revno],
                                      cwd=checkoutdir)

    def get_svn(self, p):
        checkoutdir = os.path.join(self.subdir_root, p.get('directory'))
        revno = p.get('revision')
        is_there = os.path.isdir(checkoutdir)
        if is_there:
            p, out = Popen_safe(['svn', 'info', '--show-item', 'revision', checkoutdir])
            current_revno = out
            if current_revno == revno:
                return

            if revno.lower() == 'head':
                # Failure to do pull is not a fatal error,
                # because otherwise you can't develop without
                # a working net connection.
                subprocess.call(['svn', 'update'], cwd=checkoutdir)
            else:
                subprocess.check_call(['svn', 'update', '-r', revno], cwd=checkoutdir)
        else:
            subprocess.check_call(['svn', 'checkout', '-r', revno, p.get('url'),
                                   p.get('directory')], cwd=self.subdir_root)

    def get_data(self, url):
        blocksize = 10 * 1024
        h = hashlib.sha256()
        tmpfile = tempfile.NamedTemporaryFile(mode='wb', dir=self.cachedir, delete=False)
        if url.startswith('https://wrapdb.mesonbuild.com'):
            resp = open_wrapdburl(url)
        else:
            resp = urllib.request.urlopen(url, timeout=req_timeout)
        with contextlib.closing(resp) as resp:
            try:
                dlsize = int(resp.info()['Content-Length'])
            except TypeError:
                dlsize = None
            if dlsize is None:
                print('Downloading file of unknown size.')
                while True:
                    block = resp.read(blocksize)
                    if block == b'':
                        break
                    h.update(block)
                    tmpfile.write(block)
                hashvalue = h.hexdigest()
                return hashvalue, tmpfile.name
            print('Download size:', dlsize)
            print('Downloading: ', end='')
            sys.stdout.flush()
            printed_dots = 0
            downloaded = 0
            while True:
                block = resp.read(blocksize)
                if block == b'':
                    break
                downloaded += len(block)
                h.update(block)
                tmpfile.write(block)
                ratio = int(downloaded / dlsize * 10)
                while printed_dots < ratio:
                    print('.', end='')
                    sys.stdout.flush()
                    printed_dots += 1
            print('')
            hashvalue = h.hexdigest()
        return hashvalue, tmpfile.name

    def check_hash(self, p, what, path):
        expected = p.get(what + '_hash')
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            h.update(f.read())
        dhash = h.hexdigest()
        if dhash != expected:
            raise RuntimeError('Incorrect hash for %s:\n %s expected\n %s actual.' % (what, expected, dhash))

    def download(self, p, what, ofname):
        self.check_can_download()
        srcurl = p.get(what + '_url')
        mlog.log('Downloading', mlog.bold(self.packagename), what, 'from', mlog.bold(srcurl))
        dhash, tmpfile = self.get_data(srcurl)
        expected = p.get(what + '_hash')
        if dhash != expected:
            os.remove(tmpfile)
            raise RuntimeError('Incorrect hash for %s:\n %s expected\n %s actual.' % (what, expected, dhash))
        os.rename(tmpfile, ofname)

    def get_file_internal(self, p, what):
        filename = p.get(what + '_filename')
        cache_path = os.path.join(self.cachedir, filename)

        if os.path.exists(cache_path):
            self.check_hash(p, what, cache_path)
            mlog.log('Using', mlog.bold(self.packagename), what, 'from cache.')
            return cache_path

        if not os.path.isdir(self.cachedir):
            os.mkdir(self.cachedir)
        self.download(p, what, cache_path)
        return cache_path

    def apply_patch(self, p):
        path = self.get_file_internal(p, 'patch')
        try:
            shutil.unpack_archive(path, self.subdir_root)
        except Exception:
            with tempfile.TemporaryDirectory() as workdir:
                shutil.unpack_archive(path, workdir)
                self.copy_tree(workdir, self.subdir_root)

    def copy_tree(self, root_src_dir, root_dst_dir):
        """
        Copy directory tree. Overwrites also read only files.
        """
        for src_dir, dirs, files in os.walk(root_src_dir):
            dst_dir = src_dir.replace(root_src_dir, root_dst_dir, 1)
            if not os.path.exists(dst_dir):
                os.makedirs(dst_dir)
            for file_ in files:
                src_file = os.path.join(src_dir, file_)
                dst_file = os.path.join(dst_dir, file_)
                if os.path.exists(dst_file):
                    try:
                        os.remove(dst_file)
                    except PermissionError as exc:
                        os.chmod(dst_file, stat.S_IWUSR)
                        os.remove(dst_file)
                shutil.copy2(src_file, dst_dir)
