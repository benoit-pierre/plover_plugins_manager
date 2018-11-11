from distutils import sysconfig
from path import Path
import ast
import importlib
import os
import stat
import textwrap
import venv

import pkg_resources


def DALS(s):
    "dedent and left-strip"
    return textwrap.dedent(s).lstrip()

def patch_file(filename, patch):
    with open(filename, 'r') as fp:
        contents = fp.read()
    contents = patch(contents)
    with open(filename, 'w') as fp:
        fp.write(contents)


class VirtualEnv(object):

    def __init__(self, workspace):
        self.workspace = workspace
        self.venv = workspace.workspace / 'venv'
        self.site_packages = Path(sysconfig.get_python_lib(prefix=self.venv))
        venv.create(self.venv, with_pip=False)
        # Create fake home directory.
        self.home = self.workspace.workspace / 'home'
        self.home.mkdir()
        # Create empty configuration file for Plover.
        self.plover = self.venv / 'plover'
        self.plover.mkdir()
        (self.plover / 'plover.cfg').touch()
        # Install dependencies.
        deps = set()
        def resolve_deps(dist):
            if dist in deps:
                return
            deps.add(dist)
            for req in dist.requires():
                resolve_deps(pkg_resources.get_distribution(req))
        resolve_deps(pkg_resources.get_distribution('plover_plugins_manager'))
        resolve_deps(pkg_resources.get_distribution('PyQt5'))
        for dist_name in sorted(dist.project_name for dist in deps):
            self.clone_distribution(dist_name)
        # Fixup pip so using a virtualenv is not an issue.
        pip_locations = self.site_packages / 'pip' / '_internal' / 'locations.py'
        patch_file(pip_locations, lambda s: s.replace(
            '\ndef running_under_virtualenv():\n',
            '\ndef running_under_virtualenv():'
            '\n    return False\n',
        ))
        # Set user site packages directory.
        self.user_site = Path(self.pyeval(DALS(
            '''
            import site
            print(repr(site.USER_SITE))
            '''
        ), enable_user_site=False))

    def _chmod_venv(self, add_mode, rm_mode):
        plover_path = self.plover.abspath()
        for dirpath, dirnames, filenames in os.walk(self.venv.abspath(), topdown=False):
            for p in dirnames + filenames:
                p = os.path.join(dirpath, p)
                if p == plover_path:
                    continue
                st_mode = os.lstat(p).st_mode
                if stat.S_ISLNK(st_mode):
                    continue
                old_mode = stat.S_IMODE(st_mode)
                new_mode = (old_mode | add_mode) & ~rm_mode
                # print(p, oct(old_mode), oct(new_mode))
                os.chmod(p, new_mode)

    def freeze(self):
        self._chmod_venv(0, stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)

    def thaw(self):
        self._chmod_venv(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH, 0)

    def clone_distribution(self, dist_name, verbose=False):
        """
        Clone a distribution from the current
        environment to the virtual environment.
        """
        def clone(src_path):
            dst_path = self.site_packages / src_path.name
            isdir = src_path.isdir()
            (src_path.copytree if isdir else src_path.copyfile)(dst_path)
        src_dist = pkg_resources.get_distribution(dist_name)
        # Copy distribution info.
        clone(Path(src_dist.egg_info))
        # Copy top-level modules.
        src_location = Path(src_dist.location)
        modules = list(src_dist._get_metadata('top_level.txt'))
        for modname in modules or (dist_name,):
            spec = importlib.util.find_spec(modname)
            if spec is None:
                continue
            origin = Path(spec.origin)
            if origin.name == '__init__.py':
                origin = origin.parent
            clone(origin)

    def run(self, cmd, capture=False, enable_user_site=True):
        bindir = self.venv.abspath() / 'bin'
        env = dict(os.environ)
        env.update(dict(
            HOME=str(self.home.abspath()),
            VIRTUAL_ENV=str(self.venv.abspath()),
            PATH=os.pathsep.join((bindir, env['PATH'])),
        ))
        if enable_user_site:
            env['PYTHONPATH'] = str(self.user_site.abspath())
        cmd[0] = bindir / cmd[0]
        return self.workspace.run(cmd, capture=capture, env=env,
                                  cwd=self.plover.abspath())

    def pyrun(self, args, **kwargs):
        return self.run(['python'] + list(args), **kwargs)

    def pyexec(self, script, **kwargs):
        return self.pyrun(['-c', DALS(script)], **kwargs)

    def pyeval(self, script, **kwargs):
        return ast.literal_eval(self.pyexec(script, capture=True, **kwargs))

    def install_plugins(self, args, **kwargs):
        return self.pyrun('-m plover_plugins_manager install'.split() + args, **kwargs)

    def uninstall_plugins(self, args, **kwargs):
        return self.pyrun('-m plover_plugins_manager uninstall -y'.split() + args, **kwargs)

    def list_distributions(self, directory):
        return {
            str(d.as_requirement())
            for d in pkg_resources.find_distributions(directory)
        }

    def list_all_plugins(self, **kwargs):
        return set(self.pyrun('-m plover_plugins_manager '
                              'list_plugins --freeze'.split(),
                              capture=True, **kwargs).strip().split('\n'))

    def list_user_plugins(self):
        return self.list_distributions(self.user_site)