AUTOMAKE_OPTIONS        = subdir-objects
SUBDIRS                 = src

# Help make tarballs more reproducible.
export TAR_OPTIONS      = --owner=0 --group=0 --numeric-owner

EXTRA_DIST              =                   \
  LICENSE                                   \
  MANIFEST.in                               \
  README.md                                 \
  requirements.txt                          \
  setup.py                                  \
  pyproject.toml                            \
  main.py

dist-rpm: dist
	mkdir -p rpmbuild/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
	rm -f rpmbuild/SOURCES/*
	ln -s $(abs_top_srcdir)/djehuty-@VERSION@.tar.gz rpmbuild/SOURCES/djehuty-@VERSION@.tar.gz
	rpmbuild --define "_topdir $(abs_top_srcdir)/rpmbuild" -ba $(abs_top_srcdir)/rpmbuild/SPECS/djehuty.spec

dist-docker: dist
	guix pack -C gzip -e '(load "guix.scm")'                                   \
             --with-source=djehuty-$(VERSION).tar.gz                         \
             --format=docker --entry-point=/bin/djehuty                      \
             -S /bin=bin -S /lib=lib -S /share=share -S /etc=etc -S /var=var \
             glibc-locales tzdata coreutils net-base bash virtuoso-ose

source-dist:
	@PYTHON@ setup.py sdist --formats=gztar

binary-dist:
	@PYTHON@ setup.py bdist --formats=gztar

all-local:
	@PYTHON@ setup.py build --build-base $(shell readlink -f $(builddir))/build

install-exec-local:
	@PYTHON@ setup.py install                 \
        --prefix $(DESTDIR)$(prefix)        \
        --single-version-externally-managed \
        --record $(DESTDIR)$(pkgpythondir)/install_files.txt

uninstall-local:
	cat $(DESTDIR)$(pkgpythondir)/install_files.txt | xargs rm -rf
	rm -rf $(DESTDIR)$(pkgpythondir)

lint:
	@pylint src/djehuty/* > pylint.log || true
	@printf "Wrote 'pylint.log'.\n"

coverity-report:
	@cov-build --dir cov-int --no-command --fs-capture-search .
