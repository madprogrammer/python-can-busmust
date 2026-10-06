{
  lib,
  buildPythonPackage,
  setuptools,
  python-can,
  pyusb,
  pytestCheckHook,
  can-isotp,
  cantools,
}:

buildPythonPackage {
  pname = "python-can-busmust";
  version = (builtins.fromTOML (builtins.readFile ../pyproject.toml)).project.version;
  pyproject = true;

  # Explicit source selection also excludes local credentials, virtualenvs,
  # build artifacts, and the separately licensed reference checkout.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../MANIFEST.in
      ../README.md
      ../LICENSE
      ../CHANGELOG.md
      (lib.fileset.fileFilter (file: file.hasExt "py") ../src/busmust)
      (lib.fileset.fileFilter (file: file.hasExt "py") ../tests)
      (lib.fileset.fileFilter (file: file.hasExt "py") ../examples)
      (lib.fileset.fileFilter (file: file.hasExt "md") ../docs)
    ];
  };

  build-system = [ setuptools ];
  # nixpkgs' PyUSB already patches libusb discovery to an absolute store path.
  # Its runtime closure includes libusb, without LD_LIBRARY_PATH wrappers.
  dependencies = [
    python-can
    pyusb
  ];

  nativeCheckInputs = [
    pytestCheckHook
    can-isotp
    cantools
  ];
  pythonImportsCheck = [ "busmust" ];

  meta = {
    description = "Alpha native PyUSB BUSMUST CAN and CAN FD backend for python-can";
    homepage = "https://github.com/madprogrammer/python-can-busmust";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
  };
}
