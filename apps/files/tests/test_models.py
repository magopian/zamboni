# -*- coding: utf-8 -*-
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime

from django import forms
from django.core.files.storage import default_storage as storage
from django.conf import settings

import mock
import path
from nose.tools import eq_

import amo
import amo.tests
import amo.utils
import devhub.signals

from amo.utils import rm_local_tmp_dir
from addons.models import Addon
from applications.models import Application, AppVersion
from files.models import File, FileUpload, FileValidation, nfd_str, Platform
from files.helpers import copyfileobj
from files.utils import check_rdf, JetpackUpgrader, parse_addon, parse_xpi
from versions.models import Version


class UploadTest(amo.tests.TestCase, amo.tests.AMOPaths):
    """
    Base for tests that mess with file uploads, safely using temp directories.
    """
    fixtures = ['applications/all_apps.json', 'base/appversion']

    def setUp(self):
        self._rename = path.path.rename
        path.path.rename = path.path.copy
        # The validator task (post Addon upload) loads apps.json
        # so ensure it exists:
        from django.core.management import call_command
        call_command('dump_apps')

    def tearDown(self):
        path.path.rename = self._rename

    def file_path(self, *args, **kw):
        return self.file_fixture_path(*args, **kw)

    def get_upload(self, filename=None, abspath=None, validation=None,
                   is_webapp=False):
        xpi = open(abspath if abspath else self.file_path(filename)).read()
        upload = FileUpload.from_post([xpi], filename=abspath or filename,
                                      size=1234)
        # Simulate what fetch_manifest() does after uploading an app.
        upload.is_webapp = is_webapp
        upload.validation = (validation or
                             json.dumps(dict(errors=0, warnings=1, notices=2,
                                             metadata={}, messages=[])))
        upload.save()
        return upload


class TestFile(amo.tests.TestCase, amo.tests.AMOPaths):
    """
    Tests the methods of the File model.
    """
    fixtures = ['base/addon_3615', 'base/addon_5579', 'base/platforms']

    def test_get_absolute_url(self):
        f = File.objects.get(id=67442)
        url = f.get_absolute_url(src='src')
        expected = ('/firefox/downloads/file/67442/'
                    'delicious_bookmarks-2.1.072-fx.xpi?src=src')
        assert url.endswith(expected), url

    def check_delete(self, file_, filename):
        """Test that when the File object is deleted, it is removed from the
        filesystem."""
        try:
            with storage.open(filename, 'w') as f:
                f.write('sample data\n')
            assert storage.exists(filename)
            file_.delete()
            assert not storage.exists(filename)
        finally:
            if storage.exists(filename):
                storage.delete(filename)

    def test_delete_by_version(self):
        f = File.objects.get(pk=67442)
        version = f.version
        self.check_delete(version, f.file_path)

    def test_delete_file_path(self):
        f = File.objects.get(pk=67442)
        self.check_delete(f, f.file_path)

    def test_delete_mirror_file_path(self):
        f = File.objects.get(pk=67442)
        self.check_delete(f, f.mirror_file_path)

    def test_delete_no_file(self):
        # test that the file object can be deleted without the file
        # being present
        file = File.objects.get(pk=74797)
        filename = file.file_path
        assert not os.path.exists(filename), 'File exists at: %s' % filename
        file.delete()

    def test_delete_signal(self):
        """Test that if there's no filename, the signal is ok."""
        file = File.objects.get(pk=67442)
        file.update(filename='')
        file.delete()

    @mock.patch('files.models.File.hide_disabled_file')
    def test_disable_signal(self, hide_mock):
        f = File.objects.get(pk=67442)
        f.status = amo.STATUS_PUBLIC
        f.save()
        assert not hide_mock.called

        f.status = amo.STATUS_DISABLED
        f.save()
        assert hide_mock.called

    @mock.patch('files.models.File.unhide_disabled_file')
    def test_unhide_on_enable(self, unhide_mock):
        f = File.objects.get(pk=67442)
        f.status = amo.STATUS_PUBLIC
        f.save()
        assert not unhide_mock.called

        f = File.objects.get(pk=67442)
        f.status = amo.STATUS_DISABLED
        f.save()
        assert not unhide_mock.called

        f = File.objects.get(pk=67442)
        f.status = amo.STATUS_PUBLIC
        f.save()
        assert unhide_mock.called

    def test_unhide_disabled_files(self):
        f = File.objects.get(pk=67442)
        f.status = amo.STATUS_PUBLIC
        with storage.open(f.guarded_file_path, 'wb') as fp:
            fp.write('some data\n')
        f.unhide_disabled_file()
        assert storage.exists(f.file_path)
        assert storage.open(f.file_path).size

    def test_unhide_disabled_file_mirroring(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp))
        with mock.patch.object(settings, 'MIRROR_STAGE_PATH', tmp):
            fo = File.objects.get(pk=67442)
            with storage.open(fo.file_path, 'wb') as fp:
                fp.write('<pretend this is an xpi>')
            with storage.open(fo.mirror_file_path, 'wb') as fp:
                fp.write('<pretend this is an xpi>')
            fo.status = amo.STATUS_DISABLED
            fo.save()
            assert not storage.exists(fo.file_path), 'file not hidden'
            assert not storage.exists(fo.mirror_file_path), (
                            'file not removed from mirror')

            fo = File.objects.get(pk=67442)
            fo.status = amo.STATUS_PUBLIC
            fo.save()
            assert storage.exists(fo.file_path), 'file not un-hidden'
            assert storage.exists(fo.mirror_file_path), (
                            'file not copied back to mirror')

    @mock.patch('files.models.File.copy_to_mirror')
    def test_copy_to_mirror_on_status_change(self, copy_mock):

        assert amo.STATUS_UNREVIEWED not in amo.MIRROR_STATUSES

        f = File.objects.get(pk=67442)
        f.status = amo.STATUS_UNREVIEWED
        f.save()
        assert not copy_mock.called
        copy_mock.reset_mock()

        for status in amo.MIRROR_STATUSES:
            f = File.objects.get(pk=67442)
            f.status = status
            f.save()
            assert copy_mock.called, "Copy not called"
            f.status = amo.STATUS_UNREVIEWED
            f.save()
            copy_mock.reset_mock()

    def test_latest_url(self):
        # With platform.
        f = File.objects.get(id=74797)
        base = '/firefox/downloads/latest/'
        expected = base + '{0}/platform:3/addon-{0}-latest.xpi'
        eq_(expected.format(f.version.addon_id), f.latest_xpi_url())

        # No platform.
        f = File.objects.get(id=67442)
        expected = base + '{0}/addon-{0}-latest.xpi'
        eq_(expected.format(f.version.addon_id), f.latest_xpi_url())

    def test_eula_url(self):
        f = File.objects.get(id=67442)
        eq_(f.eula_url(), '/en-US/firefox/addon/3615/eula/67442')

    def test_generate_filename(self):
        f = File.objects.get(id=67442)
        eq_(f.generate_filename(), 'delicious_bookmarks-2.1.072-fx.xpi')

    def test_generate_filename_webapp(self):
        f = File.objects.get(id=67442)
        f.version.addon.app_slug = 'testing-123'
        f.version.addon.type = amo.ADDON_WEBAPP
        eq_(f.generate_filename(), 'testing-123-2.1.072.webapp')

    def test_generate_filename_packaged_app(self):
        f = File.objects.get(id=67442)
        f.version.addon.app_slug = 'testing-123'
        f.version.addon.type = amo.ADDON_WEBAPP
        f.version.addon.is_packaged = True
        eq_(f.generate_filename(), 'testing-123-2.1.072.zip')

    def test_generate_webapp_fn_non_ascii(self):
        f = File()
        f.version = Version(version='0.1.7')
        f.version.compatible_apps = (amo.FIREFOX,)
        f.version.addon = Addon(app_slug=u' フォクすけ  といっしょ',
                                type=amo.ADDON_WEBAPP)
        eq_(f.generate_filename(), 'app-0.1.7.webapp')

    def test_generate_webapp_fn_partial_non_ascii(self):
        f = File()
        f.version = Version(version='0.1.7')
        f.version.compatible_apps = (amo.FIREFOX,)
        f.version.addon = Addon(app_slug=u'myapp フォクすけ  といっしょ',
                                type=amo.ADDON_WEBAPP)
        eq_(f.generate_filename(), 'myapp-0.1.7.webapp')

    def test_pretty_filename(self):
        f = File.objects.get(id=67442)
        f.generate_filename()
        eq_(f.pretty_filename(), 'delicious_bookmarks-2.1.072-fx.xpi')

    def test_pretty_filename_short(self):
        f = File.objects.get(id=67442)
        f.version.addon.name = 'A Place Where The Sea Remembers Your Name'
        f.generate_filename()
        eq_(f.pretty_filename(), 'a_place_where_the...-2.1.072-fx.xpi')

    def test_generate_filename_platform_specific(self):
        f = File.objects.get(id=67442)
        f.platform_id = amo.PLATFORM_MAC.id
        eq_(f.generate_filename(), 'delicious_bookmarks-2.1.072-fx-mac.xpi')

    def test_generate_filename_many_apps(self):
        f = File.objects.get(id=67442)
        f.version.compatible_apps = (amo.FIREFOX, amo.THUNDERBIRD)
        eq_(f.generate_filename(), 'delicious_bookmarks-2.1.072-fx+tb.xpi')

    def test_generate_filename_ja(self):
        f = File()
        f.version = Version(version='0.1.7')
        f.version.compatible_apps = (amo.FIREFOX,)
        f.version.addon = Addon(name=u' フォクすけ  といっしょ')
        eq_(f.generate_filename(), 'addon-0.1.7-fx.xpi')

    def clean_files(self, f):
        if f.mirror_file_path and storage.exists(f.mirror_file_path):
            storage.delete(f.mirror_file_path)
        if not storage.exists(f.file_path):
            with storage.open(f.file_path, 'w') as fp:
                fp.write('sample data\n')

    def test_copy_to_mirror(self):
        f = File.objects.get(id=67442)
        self.clean_files(f)
        f.copy_to_mirror()
        assert storage.exists(f.mirror_file_path)

    @mock.patch('shutil.copyfile')
    def test_not_copy_to_mirror(self, copyfile):
        f = File.objects.get(id=67442)
        f.version.addon.update(premium_type=amo.ADDON_PREMIUM)
        self.clean_files(f)
        f.copy_to_mirror()
        assert not f.mirror_file_path
        assert not copyfile.called

    def test_generate_hash(self):
        f = File()
        f.version = Version.objects.get(pk=81551)
        fn = self.xpi_path('delicious_bookmarks-2.1.106-fx')
        assert f.generate_hash(fn).startswith('sha256:fd277d45ab44f6240e')

    def test_public_is_testable(self):
        f = File.objects.get(pk=67442)
        f.update(status=amo.STATUS_PUBLIC)
        eq_(f.can_be_perf_tested(), True)

    def test_reviewed_is_testable(self):
        f = File.objects.get(pk=67442)
        f.update(status=amo.STATUS_LITE)
        eq_(f.can_be_perf_tested(), True)

    def test_unreviewed_is_not_testable(self):
        f = File.objects.get(pk=67442)
        f.update(status=amo.STATUS_UNREVIEWED)
        eq_(f.can_be_perf_tested(), False)

    def test_disabled_is_not_testable(self):
        f = File.objects.get(pk=67442)
        f.update(status=amo.STATUS_DISABLED)
        eq_(f.can_be_perf_tested(), False)

    def test_deleted_addon_is_not_testable(self):
        f = File.objects.get(pk=67442)
        f.version.addon.update(disabled_by_user=True)
        eq_(f.can_be_perf_tested(), False)

    def test_webapp_is_not_testable(self):
        f = File.objects.get(pk=67442)
        f.version.addon.update(type=amo.ADDON_WEBAPP)
        eq_(f.can_be_perf_tested(), False)

    def test_file_is_mirrorable(self):
        f = File.objects.get(pk=67442)
        eq_(f.is_mirrorable(), True)

        f.update(status=amo.STATUS_DISABLED)
        eq_(f.is_mirrorable(), False)

    def test_premium_addon_not_mirrorable(self):
        f = File.objects.get(pk=67442)
        f.version.addon.premium_type = amo.ADDON_PREMIUM
        eq_(f.is_mirrorable(), False)

    def test_dont_mirror_apps(self):
        f = File.objects.get(pk=67442)
        f.version.addon.update(type=amo.ADDON_WEBAPP)
        eq_(f.is_mirrorable(), False)


class TestParseXpi(amo.tests.TestCase):
    fixtures = ['base/apps']

    def setUp(self):
        for version in ('3.0', '3.6.*'):
            AppVersion.objects.create(application_id=amo.FIREFOX.id,
                                      version=version)

    def parse(self, addon=None, filename='extension.xpi'):
        path = 'apps/files/fixtures/files/' + filename
        xpi = os.path.join(settings.ROOT, path)
        return parse_addon(open(xpi), addon)

    def test_parse_basics(self):
        # Everything but the apps
        exp = {'guid': 'guid@xpi',
               'name': 'xpi name',
               'summary': 'xpi description',
               'version': '0.1',
               'homepage': 'http://homepage.com',
               'type': 1}
        parsed = self.parse()
        for key, value in exp.items():
            eq_(parsed[key], value)

    def test_parse_apps(self):
        exp = (amo.FIREFOX,
               amo.FIREFOX.id,
               AppVersion.objects.get(version='3.0'),
               AppVersion.objects.get(version='3.6.*'))
        eq_(self.parse()['apps'], [exp])

    def test_parse_apps_bad_appver(self):
        AppVersion.objects.all().delete()
        eq_(self.parse()['apps'], [])

    def test_parse_apps_bad_guid(self):
        Application.objects.all().delete()
        eq_(self.parse()['apps'], [])

    def test_guid_match(self):
        addon = Addon.objects.create(guid='guid@xpi', type=1)
        eq_(self.parse(addon)['guid'], 'guid@xpi')

    def test_guid_nomatch(self):
        addon = Addon.objects.create(guid='xxx', type=1)
        with self.assertRaises(forms.ValidationError) as e:
            self.parse(addon)
        eq_(e.exception.messages, ["UUID doesn't match add-on."])

    def test_guid_dupe(self):
        Addon.objects.create(guid='guid@xpi', type=1)
        with self.assertRaises(forms.ValidationError) as e:
            self.parse()
        eq_(e.exception.messages, ['Duplicate UUID found.'])

    def test_match_type(self):
        addon = Addon.objects.create(guid='guid@xpi', type=4)
        with self.assertRaises(forms.ValidationError) as e:
            self.parse(addon)
        eq_(e.exception.messages,
            ["<em:type> doesn't match add-on"])

    def test_xml_for_extension(self):
        addon = Addon.objects.create(guid='guid@xpi', type=1)
        with self.assertRaises(forms.ValidationError) as e:
            self.parse(addon, filename='search.xml')
        eq_(e.exception.messages, ["<em:type> doesn't match add-on"])

    def test_unknown_app(self):
        data = self.parse(filename='theme-invalid-app.jar')
        eq_(data['apps'], [])

    def test_bad_zipfile(self):
        with self.assertRaises(forms.ValidationError) as e:
            parse_addon('baxmldzip.xpi', None)
        eq_(e.exception.messages, ['Could not parse install.rdf.'])

    def test_parse_dictionary(self):
        result = self.parse(filename='dictionary-test.xpi')
        eq_(result['type'], amo.ADDON_DICT)

    def test_parse_dictionary_explicit_type(self):
        result = self.parse(filename='dictionary-explicit-type-test.xpi')
        eq_(result['type'], amo.ADDON_DICT)

    def test_parse_dictionary_extension(self):
        result = self.parse(filename='dictionary-extension-test.xpi')
        eq_(result['type'], amo.ADDON_EXTENSION)

    def test_parse_jar(self):
        result = self.parse(filename='theme.jar')
        eq_(result['type'], amo.ADDON_THEME)

    def test_parse_theme_by_type(self):
        result = self.parse(filename='theme-type.xpi')
        eq_(result['type'], amo.ADDON_THEME)

    def test_parse_theme_with_internal_name(self):
        result = self.parse(filename='theme-internal-name.xpi')
        eq_(result['type'], amo.ADDON_THEME)

    def test_parse_no_type(self):
        result = self.parse(filename='no-type.xpi')
        eq_(result['type'], amo.ADDON_EXTENSION)

    def test_parse_invalid_type(self):
        result = self.parse(filename='invalid-type.xpi')
        eq_(result['type'], amo.ADDON_EXTENSION)

    def test_parse_langpack(self):
        result = self.parse(filename='langpack.xpi')
        eq_(result['type'], amo.ADDON_LPAPP)

    def test_good_version_number(self):
        check_rdf({'guid': 'guid', 'version': '1.2a-b+32*__yeah'})
        check_rdf({'guid': 'guid', 'version': '1' * 32})

    def test_bad_version_number(self):
        with self.assertRaises(forms.ValidationError) as e:
            check_rdf({'guid': 'guid', 'version': 'bad #version'})
        msg = e.exception.messages[0]
        assert msg.startswith('Version numbers should only contain'), msg

    def test_long_version_number(self):
        with self.assertRaises(forms.ValidationError) as e:
            check_rdf({'guid': 'guid', 'version': '1' * 33})
        msg = e.exception.messages[0]
        eq_(msg, 'Version numbers should have fewer than 32 characters.')

    def test_strict_compat_undefined(self):
        result = self.parse()
        eq_(result['strict_compatibility'], False)

    def test_strict_compat_enabled(self):
        result = self.parse(filename='strict-compat.xpi')
        eq_(result['strict_compatibility'], True)


class TestParseAlternateXpi(amo.tests.TestCase, amo.tests.AMOPaths):
    # This install.rdf is completely different from our other xpis.
    fixtures = ['base/apps']

    def setUp(self):
        for version in ('3.0', '4.0b3pre'):
            AppVersion.objects.create(application_id=amo.FIREFOX.id,
                                      version=version)

    def parse(self, filename='alt-rdf.xpi'):
        return parse_addon(open(self.file_fixture_path(filename)))

    def test_parse_basics(self):
        # Everything but the apps.
        exp = {'guid': '{2fa4ed95-0317-4c6a-a74c-5f3e3912c1f9}',
               'name': 'Delicious Bookmarks',
               'summary': 'Access your bookmarks wherever you go and keep '
                              'them organized no matter how many you have.',
               'homepage': 'http://delicious.com',
               'type': amo.ADDON_EXTENSION,
               'version': '2.1.106'}
        parsed = self.parse()
        for key, value in exp.items():
            eq_(parsed[key], value)

    def test_parse_apps(self):
        exp = (amo.FIREFOX,
               amo.FIREFOX.id,
               AppVersion.objects.get(version='3.0'),
               AppVersion.objects.get(version='4.0b3pre'))
        eq_(self.parse()['apps'], [exp])

    @mock.patch('files.utils.rdflib.Graph')
    def test_no_manifest_node(self, graph_mock):
        rdf_mock = mock.Mock()
        graph_mock.return_value.parse.return_value = rdf_mock
        rdf_mock.triples.return_value = iter([])
        rdf_mock.subjects.return_value = iter([])
        with self.assertRaises(forms.ValidationError) as e:
            self.parse()
        eq_(e.exception.messages, ['Could not parse install.rdf.'])


class TestFileUpload(UploadTest):
    fixtures = ['applications/all_apps.json', 'base/appversion',
                'base/addon_3615']

    def setUp(self):
        super(TestFileUpload, self).setUp()
        self.data = 'file contents'

    def upload(self):
        # The data should be in chunks.
        data = [''.join(x) for x in amo.utils.chunked(self.data, 3)]
        return FileUpload.from_post(data, 'filename.xpi',
                                    len(self.data))

    def test_from_post_write_file(self):
        eq_(storage.open(self.upload().path).read(), self.data)

    def test_from_post_filename(self):
        eq_(self.upload().name, 'filename.xpi')

    def test_from_post_hash(self):
        hash = hashlib.sha256(self.data).hexdigest()
        eq_(self.upload().hash, 'sha256:%s' % hash)

    def test_save_without_validation(self):
        f = FileUpload.objects.create()
        assert not f.valid

    def test_save_with_validation(self):
        f = FileUpload.objects.create(
                                validation='{"errors": 0, "metadata": {}}')
        assert f.valid

        f = FileUpload.objects.create(validation='wtf')
        assert not f.valid

    def test_update_with_validation(self):
        f = FileUpload.objects.create()
        f.validation = '{"errors": 0, "metadata": {}}'
        f.save()
        assert f.valid

    def test_update_without_validation(self):
        f = FileUpload.objects.create()
        f.save()
        assert not f.valid

    def test_ascii_names(self):
        fu = FileUpload.from_post('', u'jétpack.xpi', 0)
        assert 'xpi' in fu.name

        fu = FileUpload.from_post('', u'мозила_србија-0.11-fx.xpi', 0)
        assert 'xpi' in fu.name

        fu = FileUpload.from_post('', u'フォクすけといっしょ.xpi', 0)
        assert 'xpi' in fu.name

        fu = FileUpload.from_post('', u'\u05d0\u05d5\u05e1\u05e3.xpi', 0)
        assert 'xpi' in fu.name

    def test_validator_sets_binary_via_extensions(self):
        validation = json.dumps({
            "errors": 0,
            "success": True,
            "warnings": 0,
            "notices": 0,
            "message_tree": {},
            "messages": [],
            "metadata": {
                "contains_binary_extension": True,
                "version": "1.0",
                "name": "gK0Bes Bot",
                "id": "gkobes@gkobes",
            }
        })
        upload = self.get_upload(filename='extension.xpi',
                                 validation=validation)
        version = Version.objects.filter(addon__pk=3615)[0]
        plat = Platform.objects.get(pk=amo.PLATFORM_LINUX.id)
        file_ = File.from_upload(upload, version, plat)
        eq_(file_.binary, True)

    def test_validator_sets_binary_via_content(self):
        validation = json.dumps({
            "errors": 0,
            "success": True,
            "warnings": 0,
            "notices": 0,
            "message_tree": {},
            "messages": [],
            "metadata": {
                "contains_binary_content": True,
                "version": "1.0",
                "name": "gK0Bes Bot",
                "id": "gkobes@gkobes",
            }
        })
        upload = self.get_upload(filename='extension.xpi',
                                 validation=validation)
        version = Version.objects.filter(addon__pk=3615)[0]
        plat = Platform.objects.get(pk=amo.PLATFORM_LINUX.id)
        file_ = File.from_upload(upload, version, plat)
        eq_(file_.binary, True)

    def test_validator_sets_require_chrome(self):
        validation = json.dumps({
            "errors": 0,
            "success": True,
            "warnings": 0,
            "notices": 0,
            "message_tree": {},
            "messages": [],
            "metadata": {
                "version": "1.0",
                "name": "gK0Bes Bot",
                "id": "gkobes@gkobes",
                "requires_chrome": True
            }
        })
        upload = self.get_upload(filename='extension.xpi',
                                 validation=validation)
        version = Version.objects.filter(addon__pk=3615)[0]
        plat = Platform.objects.get(pk=amo.PLATFORM_LINUX.id)
        file_ = File.from_upload(upload, version, plat)
        eq_(file_.requires_chrome, True)


class TestFileFromUpload(UploadTest):
    fixtures = ['base/apps']

    def setUp(self):
        super(TestFileFromUpload, self).setUp()
        appver = {amo.FIREFOX: ['3.0', '3.6', '3.6.*', '4.0b6'],
                  amo.MOBILE: ['0.1', '2.0a1pre']}
        for app, versions in appver.items():
            for version in versions:
                AppVersion(application_id=app.id, version=version).save()
        self.platform = Platform.objects.create(id=amo.PLATFORM_MAC.id)
        self.addon = Addon.objects.create(guid='guid@jetpack',
                                          type=amo.ADDON_EXTENSION,
                                          name='xxx')
        self.version = Version.objects.create(addon=self.addon)

    def upload(self, name):
        if os.path.splitext(name)[-1] not in ['.xml', '.xpi', '.jar']:
            name = name + '.xpi'

        v = json.dumps(dict(errors=0, warnings=1, notices=2, metadata={}))
        fname = nfd_str(self.xpi_path(name))
        if not storage.exists(fname):
            with storage.open(fname, 'w') as fs:
                copyfileobj(open(fname), fs)
        d = dict(path=fname, name=name,
                 hash='sha256:%s' % name, validation=v)
        return FileUpload.objects.create(**d)

    def test_jetpack_version(self):
        upload = self.upload('jetpack')
        f = File.from_upload(upload, self.version, self.platform)
        file_ = File.objects.get(id=f.id)
        eq_(file_.jetpack_version, '1.0b4')
        eq_(file_.builder_version, None)
        eq_(['jetpack'], [t.tag_text for t in self.addon.tags.all()])

    def test_jetpack_builder_version(self):
        upload = self.upload('jetpack_builder')
        f = File.from_upload(upload, self.version, self.platform)
        file_ = File.objects.get(id=f.id)
        eq_(file_.builder_version, '1.1.1.1')

    def test_jetpack_with_invalid_json(self):
        upload = self.upload('jetpack_invalid')
        f = File.from_upload(upload, self.version, self.platform)
        file_ = File.objects.get(id=f.id)
        eq_(file_.jetpack_version, None)
        eq_(file_.builder_version, None)
        assert not self.addon.tags.exists()

    def test_filename(self):
        upload = self.upload('jetpack')
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.filename, 'xxx-0.1-mac.xpi')

    def test_filename_no_extension(self):
        upload = self.upload('jetpack')
        # Remove the exension.
        upload.name = upload.name.rsplit('.', 1)[0]
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.filename, 'xxx-0.1-mac.xpi')

    def test_file_validation(self):
        upload = self.upload('jetpack')
        file = File.from_upload(upload, self.version, self.platform)
        fv = FileValidation.objects.get(file=file)
        eq_(fv.validation, upload.validation)
        eq_(fv.valid, True)
        eq_(fv.errors, 0)
        eq_(fv.warnings, 1)
        eq_(fv.notices, 2)

    def test_file_hash(self):
        upload = self.upload('jetpack')
        f = File.from_upload(upload, self.version, self.platform)
        assert f.hash.startswith('sha256:')
        assert len(f.hash) == 64 + 7  # 64 for hash, 7 for 'sha256:'

    def test_no_restart_true(self):
        upload = self.upload('jetpack')
        d = parse_addon(upload.path)
        f = File.from_upload(upload, self.version, self.platform, parse_data=d)
        assert f.no_restart

    def test_no_restart_dictionary(self):
        upload = self.upload('dictionary-explicit-type-test')
        d = parse_addon(upload.path)
        f = File.from_upload(upload, self.version, self.platform, parse_data=d)
        assert f.no_restart

    def test_no_restart_false(self):
        upload = self.upload('extension')
        d = parse_addon(upload.path)
        f = File.from_upload(upload, self.version, self.platform, parse_data=d)
        assert not f.no_restart

    def test_utf8(self):
        upload = self.upload(u'jétpack')
        self.version.addon.name = u'jéts!'
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.filename, u'jets-0.1-mac.xpi')

    def test_size(self):
        upload = self.upload('extension')
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.size, 2264)

    def test_size_small(self):
        upload = self.upload('alt-rdf')
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.size, 675)

    def test_beta_version_non_public(self):
        # Only public add-ons can get beta versions.
        upload = self.upload('beta-extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_LITE)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_UNREVIEWED)

    def test_public_to_beta(self):
        upload = self.upload('beta-extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_PUBLIC)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_BETA)

    def test_trusted_public_to_beta(self):
        upload = self.upload('beta-extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_PUBLIC, trusted=True)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_BETA)

    def test_public_to_unreviewed(self):
        upload = self.upload('extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_PUBLIC)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_UNREVIEWED)

    def test_trusted_public_to_public(self):
        upload = self.upload('extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_PUBLIC, trusted=True)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_PUBLIC)

    def test_lite_to_unreviewed(self):
        upload = self.upload('extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_LITE)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_UNREVIEWED)

    def test_trusted_lite_to_lite(self):
        upload = self.upload('extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_LITE, trusted=True)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_LITE)

    def test_litenominated_to_unreviewed(self):
        upload = self.upload('extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_LITE_AND_NOMINATED)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_UNREVIEWED)

    def test_trusted_litenominated_to_litenominated(self):
        upload = self.upload('extension')
        data = parse_addon(upload.path)
        self.addon.update(status=amo.STATUS_LITE_AND_NOMINATED, trusted=True)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.status, amo.STATUS_LITE_AND_NOMINATED)

    def test_file_hash_paranoia(self):
        upload = self.upload('extension')
        f = File.from_upload(upload, self.version, self.platform)
        assert f.hash.startswith('sha256:035ae07b4988711')

    def test_strict_compat(self):
        upload = self.upload('strict-compat')
        data = parse_addon(upload.path)
        f = File.from_upload(upload, self.version, self.platform, data)
        eq_(f.strict_compatibility, True)

    def test_theme_extension(self):
        upload = self.upload('theme.jar')
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.filename.endswith('.xpi'), True)

    def test_extension_extension(self):
        upload = self.upload('extension.xpi')
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.filename.endswith('.xpi'), True)
        assert not self.addon.tags.exists()

    def test_langpack_extension(self):
        upload = self.upload('langpack.xpi')
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.filename.endswith('.xpi'), True)

    def test_search_extension(self):
        upload = self.upload('search.xml')
        f = File.from_upload(upload, self.version, self.platform)
        eq_(f.filename.endswith('.xml'), True)


class TestZip(amo.tests.TestCase, amo.tests.AMOPaths):

    def test_zip(self):
        # This zip contains just one file chrome/ that we expect
        # to be unzipped as a directory, not a file.
        xpi = self.xpi_path('directory-test')

        # This is to work around: http://bugs.python.org/issue4710
        # which was fixed in Python 2.6.2. If the required version
        # of Python for zamboni goes to 2.6.2 or above, this can
        # be removed.
        try:
            dest = tempfile.mkdtemp()
            zipfile.ZipFile(xpi).extractall(dest)
            assert os.path.isdir(os.path.join(dest, 'chrome'))
        finally:
            rm_local_tmp_dir(dest)


class TestParseSearch(amo.tests.TestCase, amo.tests.AMOPaths):

    def parse(self, filename='search.xml'):
        return parse_addon(open(self.file_fixture_path(filename)))

    def extract(self):
        # This is the expected return value from extract_search.
        return {'url': {u'type': u'text/html', u'template':
                        u'http://www.yyy.com?q={searchTerms}'},
                'xmlns': u'http://a9.com/-/spec/opensearch/1.1/',
                'name': u'search tool',
                'description': u'Search Engine for Firefox'}

    def test_basics(self):
        # This test breaks if the day changes. Have fun with that!
        eq_(self.parse(), {
            'guid': None,
            'name': 'search tool',
            'version': datetime.now().strftime('%Y%m%d'),
            'summary': 'Search Engine for Firefox',
            'type': amo.ADDON_SEARCH})

    @mock.patch('files.utils.extract_search')
    def test_extract_search_error(self, extract_mock):
        extract_mock.side_effect = Exception
        with self.assertRaises(forms.ValidationError) as e:
            self.parse()
        assert e.exception.messages[0].startswith('Could not parse ')


@mock.patch('files.utils.parse_xpi')
@mock.patch('files.utils.parse_search')
def test_parse_addon(search_mock, xpi_mock):
    parse_addon('file.xpi', None)
    xpi_mock.assert_called_with('file.xpi', None)

    parse_addon('file.xml', None)
    search_mock.assert_called_with('file.xml', None)

    parse_addon('file.jar', None)
    xpi_mock.assert_called_with('file.jar', None)


def test_parse_xpi():
    """Fire.fm can sometimes give us errors.  Let's prevent that."""
    firefm = os.path.join(settings.ROOT,
                          'apps/files/fixtures/files/firefm.xpi')
    rdf = parse_xpi(open(firefm))
    eq_(rdf['name'], 'Fire.fm')


class TestCheckJetpackVersion(amo.tests.TestCase):
    fixtures = ['base/addon_3615']

    def setUp(self):
        self.addon = Addon.objects.get(id=3615)
        JetpackUpgrader().jetpack_versions('1.0', '1.1')

    @mock.patch('files.tasks.start_upgrade.delay')
    def test_upgrade(self, upgrade_mock):
        File.objects.update(jetpack_version='1.0')
        ids = list(File.objects.values_list('id', flat=True))
        devhub.signals.submission_done.send(sender=self.addon)
        upgrade_mock.assert_called_with(ids, priority='high')

    @mock.patch('files.tasks.start_upgrade.delay')
    def test_no_upgrade(self, upgrade_mock):
        File.objects.update(jetpack_version=None)
        devhub.signals.submission_done.send(sender=self.addon)
        assert not upgrade_mock.called

        File.objects.update(jetpack_version='0.9')
        devhub.signals.submission_done.send(sender=self.addon)
        assert not upgrade_mock.called


class LanguagePackBase(UploadTest):

    def setUp(self):
        super(LanguagePackBase, self).setUp()
        self.addon = Addon.objects.create(type=amo.ADDON_LPAPP)
        self.platform = Platform.objects.create(id=amo.PLATFORM_ALL.id)
        self.version = Version.objects.create(addon=self.addon)
        self.addon.update(status=amo.STATUS_PUBLIC)
        self.addon._current_version = self.version


class TestLanguagePack(LanguagePackBase):

    def file_create(self, path):
        return (File.objects.create(platform=self.platform,
                                    version=self.version,
                                    filename=self.xpi_path(path)))

    def test_extract(self):
        obj = self.file_create('langpack-localepicker')
        assert 'title=Select a language' in obj.get_localepicker()

    def test_extract_no_chrome_manifest(self):
        obj = self.file_create('langpack')
        eq_(obj.get_localepicker(), '')

    def test_zip_invalid(self):
        obj = self.file_create('search.xml')
        eq_(obj.get_localepicker(), '')

    @mock.patch('files.utils.SafeUnzip.extract_path')
    def test_no_locale_browser(self, extract_path):
        extract_path.return_value = 'some garbage'
        obj = self.file_create('langpack-localepicker')
        eq_(obj.get_localepicker(), '')

    @mock.patch('files.utils.SafeUnzip.extract_path')
    def test_corrupt_locale_browser_path(self, extract_path):
        extract_path.return_value = 'locale browser de woot?!'
        obj = self.file_create('langpack-localepicker')
        eq_(obj.get_localepicker(), '')
        extract_path.return_value = 'locale browser de woo:t?!as'
        # Result should be 'locale browser de woo:t?!as', but we have caching.
        eq_(obj.get_localepicker(), '')

    @mock.patch('files.utils.SafeUnzip.extract_path')
    def test_corrupt_locale_browser_data(self, extract_path):
        extract_path.return_value = 'locale browser de jar:install.rdf!foo'
        obj = self.file_create('langpack-localepicker')
        eq_(obj.get_localepicker(), '')

    def test_hits_cache(self):
        obj = self.file_create('langpack-localepicker')
        assert 'title=Select a language' in obj.get_localepicker()
        obj.update(filename='garbage')
        assert 'title=Select a language' in obj.get_localepicker()

    @mock.patch('files.models.File.get_localepicker')
    def test_cache_on_create(self, get_localepicker):
        self.file_create('langpack-localepicker')
        assert get_localepicker.called

    @mock.patch('files.models.File.get_localepicker')
    def test_cache_not_on_create(self, get_localepicker):
        self.addon.update(type=amo.ADDON_DICT)
        self.file_create('langpack-localepicker')
        assert not get_localepicker.called


class TestSignedPath(amo.tests.TestCase):
    fixtures = ['webapps/337141-steamcube']

    def setUp(self):
        self.file_ = File.objects.get(pk=81555)

    def test_path(self):
        path = (self.file_.file_path
                    .replace('.webapp', '.signed.webapp')
                    .replace(settings.ADDONS_PATH, settings.SIGNED_APPS_PATH))
        eq_(self.file_.signed_file_path, path)

    def test_reviewer_path(self):
        path = (self.file_.file_path
                    .replace('.webapp', '.signed.webapp')
                    .replace(settings.ADDONS_PATH,
                             settings.SIGNED_APPS_REVIEWER_PATH))
        eq_(self.file_.signed_reviewer_file_path, path)
