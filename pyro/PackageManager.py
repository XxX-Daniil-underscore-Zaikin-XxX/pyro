import fnmatch
import glob
import logging
import os
import shutil
import sys
import typing
import zipfile

from lxml import etree

from pyro.CommandArguments import CommandArguments
from pyro.PapyrusProject import PapyrusProject
from pyro.PathHelper import PathHelper
from pyro.ProcessManager import ProcessManager
from pyro.ProjectOptions import ProjectOptions


class PackageManager:
    log: logging.Logger = logging.getLogger('pyro')

    ppj: PapyrusProject = None
    options: ProjectOptions = None
    pak_extension: str = ''
    zip_extension: str = ''

    def __init__(self, ppj: PapyrusProject) -> None:
        self.ppj = ppj
        self.options = ppj.options

        self.pak_extension = '.ba2' if self.options.game_type == 'fo4' else '.bsa'
        self.zip_extension = '.zip'

    @staticmethod
    def _check_write_permission(file_path: str) -> None:
        if os.path.isfile(file_path):
            try:
                open(file_path, 'a').close()
            except PermissionError:
                PackageManager.log.error(f'Cannot create file without write permission to: "{file_path}"')
                sys.exit(1)

    @staticmethod
    def _generate_include_paths(includes_node: etree.ElementBase, root_path: str) -> typing.Generator:
        for include_node in includes_node:
            if not include_node.tag.endswith('Include'):
                continue

            no_recurse: bool = include_node.get('NoRecurse') == 'True'
            wildcard_pattern: str = '*' if no_recurse else r'**\*'

            if include_node.text.startswith(os.pardir):
                PackageManager.log.warning(f'Include paths cannot start with "{os.pardir}"')
                continue

            if include_node.text == os.curdir or include_node.text.startswith(os.curdir):
                include_node.text = include_node.text.replace(os.curdir, root_path, 1)

            # normalize path
            path_or_pattern = os.path.normpath(include_node.text)

            # populate files list using simple glob patterns
            if '*' in path_or_pattern:
                if not os.path.isabs(path_or_pattern):
                    search_path = os.path.join(root_path, wildcard_pattern)
                elif root_path in path_or_pattern:
                    search_path = path_or_pattern
                else:
                    PackageManager.log.warning(f'Cannot include path outside RootDir: "{path_or_pattern}"')
                    continue

                for include_path in glob.iglob(search_path, recursive=not no_recurse):
                    if os.path.isfile(include_path) and fnmatch.fnmatch(include_path, path_or_pattern):
                        yield include_path

            # populate files list using absolute paths
            elif os.path.isabs(path_or_pattern):
                if root_path not in path_or_pattern:
                    PackageManager.log.warning(f'Cannot include path outside RootDir: "{path_or_pattern}"')
                    continue

                if os.path.isfile(path_or_pattern):
                    yield path_or_pattern
                else:
                    search_path = os.path.join(path_or_pattern, wildcard_pattern)
                    yield from PathHelper.find_include_paths(search_path, no_recurse)

            else:
                # populate files list using relative file path
                test_path = os.path.join(root_path, path_or_pattern)
                if not os.path.isdir(test_path):
                    yield test_path

                # populate files list using relative folder path
                else:
                    search_path = os.path.join(root_path, path_or_pattern, wildcard_pattern)
                    yield from PathHelper.find_include_paths(search_path, no_recurse)

    def _fix_package_extension(self, package_name: str) -> str:
        if not package_name.casefold().endswith(('.ba2', '.bsa')):
            return f'{package_name}{self.pak_extension}'
        return f'{os.path.splitext(package_name)[0]}{self.pak_extension}'

    def _fix_zip_extension(self, zip_name: str) -> str:
        if not zip_name.casefold().endswith('.zip'):
            return f'{zip_name}{self.zip_extension}'
        return f'{os.path.splitext(zip_name)[0]}{self.zip_extension}'

    def build_commands(self, containing_folder: str, output_path: str) -> str:
        """
        Builds command for creating package with BSArch
        """
        arguments = CommandArguments()

        arguments.append(self.options.bsarch_path, enquote_value=True)
        arguments.append('pack')
        arguments.append(containing_folder, enquote_value=True)
        arguments.append(output_path, enquote_value=True)

        if self.options.game_type == 'fo4':
            arguments.append('-fo4')
        elif self.options.game_type == 'sse':
            arguments.append('-sse')
        else:
            arguments.append('-tes5')

        return arguments.join()

    def create_packages(self) -> None:
        # clear temporary data
        if os.path.isdir(self.options.temp_path):
            shutil.rmtree(self.options.temp_path, ignore_errors=True)

        # ensure package path exists
        if not os.path.isdir(self.options.package_path):
            os.makedirs(self.options.package_path, exist_ok=True)

        package_names: list = []

        for i, package_node in enumerate(self.ppj.packages_node):
            if not package_node.tag.endswith('Package'):
                continue

            file_name: str = package_node.get('Name')

            # prevent clobbering files previously created in this session
            if file_name.casefold() in package_names:
                file_name = f'{self.ppj.project_name} ({i})'

            if file_name.casefold() not in package_names:
                package_names.append(file_name.casefold())

            file_name = self._fix_package_extension(file_name)

            file_path: str = os.path.join(self.options.package_path, file_name)

            self._check_write_permission(file_path)

            PackageManager.log.info(f'Creating "{file_name}"...')

            for source_path in self._generate_include_paths(package_node, package_node.get('RootDir')):
                PackageManager.log.info(f'+ "{source_path}"')

                relpath = os.path.relpath(source_path, package_node.get('RootDir'))
                target_path = os.path.join(self.options.temp_path, relpath)

                # fix target path if user passes a deeper package root (RootDir)
                if source_path.casefold().endswith('.pex') and not relpath.casefold().startswith('scripts'):
                    target_path = os.path.join(self.options.temp_path, 'Scripts', relpath)

                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(source_path, target_path)

            # run bsarch
            command: str = self.build_commands(self.options.temp_path, file_path)
            ProcessManager.run_bsarch(command)

            # clear temporary data
            if os.path.isdir(self.options.temp_path):
                shutil.rmtree(self.options.temp_path, ignore_errors=True)

    def create_zip(self) -> None:
        # ensure zip output path exists
        if not os.path.isdir(self.options.zip_output_path):
            os.makedirs(self.options.zip_output_path, exist_ok=True)

        zip_names: list = []

        for i, zip_node in enumerate(self.ppj.zip_files_node):
            if not zip_node.tag.endswith('ZipFile'):
                continue

            file_name: str = zip_node.get('Name')

            # prevent clobbering files previously created in this session
            if file_name.casefold() in zip_names:
                file_name = f'{file_name} ({i})'

            if file_name.casefold() not in zip_names:
                zip_names.append(file_name.casefold())

            file_name = self._fix_zip_extension(file_name)

            file_path: str = os.path.join(self.options.zip_output_path, file_name)

            self._check_write_permission(file_path)

            compress_type: int = zipfile.ZIP_STORED if zip_node.get('Compression') == 'store' else zipfile.ZIP_DEFLATED

            zip_root_path: str = zip_node.get('RootDir')

            PackageManager.log.info(f'Creating "{file_name}"...')

            # try to resolve relative RootDir path to absolute path
            if not os.path.isabs(zip_root_path):
                test_path: str = os.path.normpath(os.path.join(self.ppj.project_path, zip_root_path))

                if os.path.isdir(test_path):
                    zip_root_path = test_path
                else:
                    PapyrusProject.log.error(f'Cannot resolve RootDir path to existing folder: "{zip_root_path}"')
                    sys.exit(1)

            try:
                with zipfile.ZipFile(file_path, mode='w', compression=compress_type) as z:
                    for include_path in self._generate_include_paths(zip_node, zip_root_path):
                        PackageManager.log.info(f'+ "{include_path}"')

                        if zip_root_path not in include_path:
                            PackageManager.log.warning(f'Cannot add file to ZIP outside RootDir: "{include_path}"')
                            continue

                        arcname: str = os.path.relpath(include_path, zip_root_path)
                        z.write(include_path, arcname, compress_type=compress_type)

                PackageManager.log.info(f'Wrote ZIP file: "{file_path}"')
            except PermissionError:
                PackageManager.log.error(f'Cannot open ZIP file for writing: "{file_path}"')
                sys.exit(1)
