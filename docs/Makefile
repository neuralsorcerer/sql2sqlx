# Minimal Sphinx Makefile.
SPHINXBUILD ?= sphinx-build
SOURCEDIR    = .
BUILDDIR     = _build

html:
	$(SPHINXBUILD) -b html "$(SOURCEDIR)" "$(BUILDDIR)/html"

clean:
	rm -rf "$(BUILDDIR)"

.PHONY: html clean


