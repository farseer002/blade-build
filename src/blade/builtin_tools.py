# Copyright (c) 2017 Tencent Inc.
# All rights reserved.
#
# Author: Li Wenting <wentingli@tencent.com>
# Date:   October 27, 2017

"""
This module defines various build functions for building
targets from sources and custom parameters.
The build function is defined as follows:

    def build_function_name(kwargs..., args):
        pass

    Return None on success, otherwise a non-zero value to
    indicate failure.

    Parameters:
        * kwargs...: name=value pairs as parameters in command line
        * args: any other non-kw args
    When call this from the command line, all arguments which match `--name=value`
    pattern will be converted into a kwarg, any other arguments merged into the
    `args` argument.
"""

from __future__ import absolute_import
from __future__ import print_function

import fnmatch
import getpass
import os
import shutil
import socket
import sys
import tarfile
import textwrap
import traceback
import time
import zipfile

from blade import blade_util
from blade import console
from blade import fatjar


def parse_command_line(argv):
    """Simple command line parsing.

    options can only be passed as the form of `--name=value`, any other arguments are treated as
    normal arguments.

    Returns:
        tuple(options: dict, args: list)
    """
    options = {}
    args = []
    for arg in argv:
        if arg.startswith('--'):
            pos = arg.find('=')
            if pos < 0:
                args.append(arg)
                continue
            name = arg[2:pos]
            value = arg[pos+1:]
            options[name] = value
        else:
            args.append(arg)
    return options, args


def generate_scm(scm, revision, url, profile, compiler, args):
    """Generate `scm.c` file"""
    version = '%s@%s' % (url, revision)
    with open(scm, 'w') as f:
        f.write(textwrap.dedent(r'''\
                /* This file was generated by blade */
                extern "C" {
                namespace binary_version {
                extern const int kSvnInfoCount = 1;
                extern const char* const kSvnInfo[] = {"%s\n"};
                extern const int kScmInfoCount = 1;
                extern const char* const kScmInfo[] = {"%s\n"};
                extern const char kBuildType[] = "%s";
                extern const char kBuildTime[] = "%s";
                extern const char kBuilderName[] = "%s";
                extern const char kHostName[] = "%s";
                extern const char kCompiler[] = "%s";
                }}''') % (version,
                          version,
                          profile,
                          time.asctime(),
                          getpass.getuser(),
                          socket.gethostname(),
                          compiler))


_PACKAGE_MANIFEST = 'MANIFEST.TXT'


def archive_package_sources(package, sources, destinations):
    """Content of the `MANIFEST.TXT` file in the target zip file"""
    manifest = []
    for i, s in enumerate(sources):
        package(s, destinations[i])
        manifest.append('%s %s' % (blade_util.md5sum_file(s), destinations[i]))
    return manifest


def generate_zip_package(path, sources, destinations):
    zip = zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED)
    manifest = archive_package_sources(zip.write, sources, destinations)
    zip.writestr(_PACKAGE_MANIFEST, '\n'.join(manifest) + '\n')
    zip.close()


_TAR_WRITE_MODES = {
    'tar': 'w',
    'tar.gz': 'w:gz',
    'tgz': 'w:gz',
    'tar.bz2': 'w:bz2',
    'tbz': 'w:bz2',
}


def generate_tar_package(path, sources, destinations, suffix):
    mode = _TAR_WRITE_MODES[suffix]
    tar = tarfile.open(path, mode, dereference=True)
    manifest = archive_package_sources(tar.add, sources, destinations)
    manifest_path = '%s.MANIFEST' % path
    m = open(manifest_path, 'w')
    m.write('\n'.join(manifest) + '\n\n')
    m.close()
    tar.add(manifest_path, _PACKAGE_MANIFEST)
    tar.close()


def generate_package(args):
    path = args[0]
    manifest = args[1:]
    assert len(manifest) % 2 == 0
    middle = len(manifest) / 2
    sources = manifest[:middle]
    destinations = manifest[middle:]
    if path.endswith('.zip'):
        generate_zip_package(path, sources, destinations)
    else:
        for ext in _TAR_WRITE_MODES:
            if path.endswith(ext):
                suffix = ext
                break
        generate_tar_package(path, sources, destinations, suffix)


def generate_securecc_object(args):
    obj, phony_obj = args
    if not os.path.exists(obj):
        shutil.copy(phony_obj, obj)
    else:
        digest = blade_util.md5sum_file(obj)
        phony_digest = blade_util.md5sum_file(phony_obj)
        if digest != phony_digest:
            shutil.copy(phony_obj, obj)


def _generate_resource_index(targets, sources, name, path):
    """Generate resource index description file for a cc resource library"""
    header, source = targets
    with open(header, 'w') as h, open(source, 'w') as c:
        full_name = blade_util.regular_variable_name(os.path.join(path, name))
        guard_name = 'BLADE_RESOURCE_%s_H_' % full_name.upper()
        index_name = 'RESOURCE_INDEX_%s' % full_name

        h.write(textwrap.dedent('''\
                // This file was automatically generated by blade
                #ifndef {0}
                #define {0}

                #ifdef __cplusplus
                extern "C" {{
                #endif

                #ifndef BLADE_RESOURCE_TYPE_DEFINED
                #define BLADE_RESOURCE_TYPE_DEFINED
                struct BladeResourceEntry {{
                    const char* name;
                    const char* data;
                    unsigned int size;
                }};
                #endif''').format(guard_name))
        c.write(textwrap.dedent('''\
                // This file was automatically generated by blade
                #include "{0}"

                const struct BladeResourceEntry {1}[] = {{''').format(header, index_name))

        for s in sources:
            entry_var = blade_util.regular_variable_name(s)
            entry_name = os.path.relpath(s, path)
            entry_size = os.path.getsize(s)
            h.write('// %s\n' % entry_name)
            h.write('extern const char RESOURCE_%s[%d];\n' % (entry_var, entry_size))
            h.write('extern const unsigned RESOURCE_%s_len;\n' % entry_var)
            c.write('    { "%s", RESOURCE_%s, %s },\n' % (entry_name, entry_var, entry_size))

        c.write(textwrap.dedent('''\
                }};
                const unsigned {0}_len = {1};''').format(index_name, len(sources)))
        h.write(textwrap.dedent('''\
                // Resource index
                extern const struct BladeResourceEntry {0}[];
                extern const unsigned {0}_len;

                #ifdef __cplusplus
                }}  // extern "C"
                #endif

                #endif  // {1}''').format(index_name, guard_name))


def generate_resource_index(args):
    name, path = args[0], args[1]
    targets = args[2], args[3]
    sources = args[4:]
    return _generate_resource_index(targets, sources, name, path)


def generate_java_jar(args):
    jar, target = args[0], args[1]
    resources_dir = target.replace('.jar', '.resources')
    arg = args[2]
    if arg.endswith('__classes__.jar'):
        classes_jar = arg
        resources = args[3:]
    else:
        classes_jar = ''
        resources = args[2:]

    def archive_resources(resources_dir, resources, new=True):
        if new:
            option = 'cf'
        else:
            option = 'uf'
        cmd = ['%s %s %s' % (jar, option, target)]
        for resource in resources:
            cmd.append("-C '%s' '%s'" % (resources_dir,
                                         os.path.relpath(resource, resources_dir)))
        return blade_util.shell(cmd)

    if classes_jar:
        shutil.copy2(classes_jar, target)
        if resources:
            return archive_resources(resources_dir, resources, False)
    else:
        return archive_resources(resources_dir, resources, True)


def generate_java_resource(args):
    assert len(args) % 2 == 0
    middle = len(args) / 2
    targets = args[:middle]
    sources = args[middle:]
    for i in range(middle):
        shutil.copy(sources[i], targets[i])


def _get_all_test_class_names_in_jar(jar):
    """Returns a list of test class names in the jar file."""
    test_class_names = []
    zip_file = zipfile.ZipFile(jar, 'r')
    name_list = zip_file.namelist()
    for name in name_list:
        basename = os.path.basename(name)
        # Exclude inner class and Test.class
        if (basename.endswith('Test.class') and
                len(basename) > len('Test.class') and
                not '$' in basename):
            class_name = name.replace('/', '.')[:-6]  # Remove .class suffix
            test_class_names.append(class_name)
    zip_file.close()
    return test_class_names


def _jacoco_test_coverage_flag(jacocoagent, packages_under_test):
    if packages_under_test and jacocoagent:
        jacocoagent = os.path.abspath(jacocoagent)
        packages = packages_under_test.split(':')
        options = [
            'includes=%s' % ':'.join([p + '.*' for p in packages if p]),
            'output=file',
        ]
        return '-javaagent:%s=%s' % (jacocoagent, ','.join(options))
    return ''


def generate_java_test(script, main_class, jacocoagent, packages_under_test, args):
    jars = args
    test_jar = jars[0]
    test_classes = ' '.join(_get_all_test_class_names_in_jar(test_jar))
    with open(script, 'w') as f:
        coverage_flags = _jacoco_test_coverage_flag(jacocoagent, packages_under_test)
        f.write(textwrap.dedent('''\
                #!/bin/sh
                # Auto generated wrapper shell script by blade

                if [ -n "$BLADE_COVERAGE" ]; then
                    coverage_options="%s"
                fi

                exec java $coverage_options -classpath %s %s %s $@''') % (
                coverage_flags, ':'.join(jars), main_class, test_classes))
    os.chmod(script, 0o755)


def generate_fat_jar(args):
    jar = args[0]
    console.set_log_file('%s.log' % jar.replace('.fat.jar', '__fatjar__'))
    console.enable_color(True)
    fatjar.generate_fat_jar(jar, args[1:])


def generate_one_jar(onejar, main_class, bootjar, args):
    # Assume the first jar is the main jar, others jars are dependencies.
    main_jar = args[0]
    jars = args[1:]
    path = onejar
    onejar = zipfile.ZipFile(path, 'w')
    jar_path_set = set()
    # Copy files from one-jar-boot.jar to the target jar
    zip_file = zipfile.ZipFile(bootjar, 'r')
    name_list = zip_file.namelist()
    for name in name_list:
        if not name.lower().endswith('manifest.mf'):  # Exclude manifest
            onejar.writestr(name, zip_file.read(name))
            jar_path_set.add(name)
    zip_file.close()

    # Main jar and dependencies
    onejar.write(main_jar, os.path.join('main',
                                        os.path.basename(main_jar)))
    for dep in jars:
        dep_name = os.path.basename(dep)
        onejar.write(dep, os.path.join('lib', dep_name))

    # Copy resources to the root of target onejar
    for jar in [main_jar] + jars:
        jar = zipfile.ZipFile(jar, 'r')
        jar_name_list = jar.namelist()
        for name in jar_name_list:
            if name.endswith('.class') or name.upper().startswith('META-INF'):
                continue
            if name not in jar_path_set:
                jar_path_set.add(name)
                onejar.writestr(name, jar.read(name))
        jar.close()

    # Manifest
    # Note that the manifest file must end with a new line or carriage return
    onejar.writestr(os.path.join('META-INF', 'MANIFEST.MF'),
                    textwrap.dedent('''\
                            Manifest-Version: 1.0
                            Main-Class: com.simontuffs.onejar.Boot
                            One-Jar-Main-Class: %s

                            ''') % main_class)
    onejar.close()


def generate_java_binary(args):
    script, onejar = args
    basename = os.path.basename(onejar)
    fullpath = os.path.abspath(onejar)
    with open(script, 'w') as f:
        f.write(textwrap.dedent('''\
                #!/bin/sh
                # Auto generated wrapper shell script by blade

                jar=`dirname "$0"`/"%s"
                if [ ! -f "$jar" ]; then
                  jar="%s"
                fi

                exec java -jar "$jar" $@
                ''') % (basename, fullpath))
    os.chmod(script, 0o755)


def generate_scala_test(script, java, scala, jacocoagent, packages_under_test, args):
    jars = args
    test_jar = jars[0]
    test_class_names = _get_all_test_class_names_in_jar(test_jar)
    scala, java = os.path.abspath(scala), os.path.abspath(java)
    java_args = ''
    coverage_flags = _jacoco_test_coverage_flag(jacocoagent, packages_under_test)
    if coverage_flags:
        java_args = '-J%s' % coverage_flags
    run_args = 'org.scalatest.run ' + ' '.join(test_class_names)
    with open(script, 'w') as f:
        text = textwrap.dedent('''\
                #!/bin/sh
                # Auto generated wrapper shell script by blade

                if [ -n "$BLADE_COVERAGE" ]; then
                    coverage_options="%s"
                fi

                JAVACMD=%s exec %s "$coverage_options" -classpath %s %s $@
                ''') % (java_args, java, scala, ':'.join(jars), run_args)
        f.write(text)
    os.chmod(script, 0o755)


def generate_shell_test(args):
    wrapper = args[0]
    scripts = args[1:]
    with open(wrapper, 'w') as f:
        f.write(textwrap.dedent("""\
                #!/bin/sh
                # Auto generated wrapper shell script by blade

                set -e

                %s

                """) % '\n'.join(['. %s' % os.path.abspath(s) for s in scripts]))
    os.chmod(wrapper, 0o755)


def generate_shell_testdata(args):
    path = args[0]
    testdata = args[1:]
    assert len(testdata) % 2 == 0
    middle = len(testdata) / 2
    sources = testdata[:middle]
    destinations = testdata[middle:]
    with open(path, 'w') as f:
        for i in range(middle):
            f.write('%s %s\n' % (os.path.abspath(sources[i]), destinations[i]))


def generate_python_library(pylib, basedir, args):
    sources = []
    for py in args:
        digest = blade_util.md5sum_file(py)
        sources.append((py, digest))
    with open(pylib, 'w') as f:
        print(str({
            'base_dir': basedir,
            'srcs': sources
        }), file=f)


def _is_python_excluded_path(filename, exclusions):
    for exclusion in exclusions:
        if fnmatch.fnmatch(filename, exclusion):
            return True
    return False


def _update_init_py_dirs(arcname, dirs, dirs_with_init_py):
    dir = os.path.dirname(arcname)
    if os.path.basename(arcname) == '__init__.py':
        dirs_with_init_py.add(dir)
    while dir:
        dirs.add(dir)
        dir = os.path.dirname(dir)


def _pybin_add_pylib(pybin, libname, exclusions, dirs, dirs_with_init_py):
    with open(libname) as pylib:
        data = eval(pylib.read())  # pylint: disable=eval-used
        pylib_base_dir = data['base_dir']
        for libsrc, digest in data['srcs']:
            arcname = os.path.relpath(libsrc, pylib_base_dir)
            if not _is_python_excluded_path(arcname, exclusions):
                _update_init_py_dirs(arcname, dirs, dirs_with_init_py)
                pybin.write(libsrc, arcname)


def _pybin_add_zip(pybin, libname, filter, exclusions, dirs, dirs_with_init_py):
    with zipfile.ZipFile(libname, 'r') as lib:
        name_list = lib.namelist()
        for name in name_list:
            if filter(name) and not _is_python_excluded_path(name, exclusions):
                if dirs is not None and dirs_with_init_py is not None:
                    _update_init_py_dirs(name, dirs, dirs_with_init_py)
                pybin.writestr(name, lib.read(name))


def _pybin_add_egg(pybin, libname, exclusions):
    def filter(name):
        if name.startswith('EGG-INFO'):
            return False
        if name.endswith('.pyc'):
            return False
        return True

    _pybin_add_zip(pybin, libname, filter, exclusions, None, None)


def _pybin_add_whl(pybin, libname, exclusions, dirs, dirs_with_init_py):
    def filter(name):
        if '.dist-info/' in name:
            return False
        return True

    _pybin_add_zip(pybin, libname, filter, exclusions, dirs, dirs_with_init_py)


def generate_python_binary(pybin, basedir, exclusions, mainentry, args):
    pybin_zip = zipfile.ZipFile(pybin, 'w', zipfile.ZIP_DEFLATED)
    exclusions = exclusions.split(',')
    dirs, dirs_with_init_py = set(), set()
    for arg in args:
        if arg.endswith('.pylib'):
            _pybin_add_pylib(pybin_zip, arg, exclusions, dirs, dirs_with_init_py)
        elif arg.endswith('.egg'):
            _pybin_add_egg(pybin_zip, arg, exclusions)
        elif arg.endswith('.whl'):
            _pybin_add_whl(pybin_zip, arg, exclusions, dirs, dirs_with_init_py)
        else:
            assert False, 'Unknown file type "%s" to build python_binary' % arg

    # Insert __init__.py into each dir if missing
    dirs_missing_init_py = dirs - dirs_with_init_py
    for dir in sorted(dirs_missing_init_py):
        pybin_zip.writestr(os.path.join(dir, '__init__.py'), '')
    pybin_zip.writestr('__init__.py', '')
    pybin_zip.close()

    with open(pybin, 'rb') as f:
        zip_content = f.read()
    # Insert bootstrap before zip, it is also a valid zip file.
    # unzip will seek actually start until meet the zip magic number.
    bootstrap = ('#!/bin/sh\n\n'
                 'PYTHONPATH="$0:$PYTHONPATH" exec python -m "%s" "$@"\n') % mainentry
    with open(pybin, 'wb') as f:
        f.write(bootstrap)
        f.write(zip_content)
    os.chmod(pybin, 0o755)


_BUILTIN_TOOLS = {
    'scm': generate_scm,
    'package': generate_package,
    'securecc_object': generate_securecc_object,
    'resource_index': generate_resource_index,
    'java_jar': generate_java_jar,
    'java_resource': generate_java_resource,
    'java_test': generate_java_test,
    'java_fatjar': generate_fat_jar,
    'java_onejar': generate_one_jar,
    'java_binary': generate_java_binary,
    'scala_test': generate_scala_test,
    'shell_test': generate_shell_test,
    'shell_testdata': generate_shell_testdata,
    'python_library': generate_python_library,
    'python_binary': generate_python_binary,
}


def main():
    name = sys.argv[1]
    try:
        options, args = parse_command_line(sys.argv[2:])
        ret = _BUILTIN_TOOLS[name](args=args, **options)
    except Exception as e:  # pylint: disable=broad-except
        ret = 1
        console.error('Blade build tool %s error: %s %s' % (name, str(e), traceback.format_exc()))
    if ret:
        sys.exit(ret)


if __name__ == '__main__':
    main()
