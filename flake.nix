{
  description = "Native PyUSB BUSMUST CAN/CAN FD driver for python-can";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          overlays = [ self.overlays.default ];
        };
    in
    {
      # Extend the Python package set, so consumers can choose their interpreter
      # and combine this driver with other Python libraries in withPackages.
      overlays.default = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (
            pythonFinal: pythonPrev:
            {
              busmust = pythonFinal.callPackage ./nix/package.nix { };
            }
            // prev.lib.optionalAttrs prev.stdenv.hostPlatform.isDarwin {
              # This pure-Python cantools dependency is incorrectly marked as
              # Linux-only in 26.05. Keep its upstream unit tests enabled.
              crccheck = pythonPrev.crccheck.overridePythonAttrs (old: {
                meta = old.meta // {
                  platforms = old.meta.platforms ++ prev.lib.platforms.darwin;
                };
              });
            }
          )
        ];
      };

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.python3Packages.busmust;
          busmust = pkgs.python3Packages.busmust;
          python = pkgs.python3.withPackages (ps: [ ps.busmust ]);
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: [
                ps.busmust
                ps.pytest
                ps.build
                ps.twine
                ps.can-isotp
                ps.cantools
              ]))
              pkgs.ruff
            ];
            # Keep installed entry-point metadata while testing local edits.
            shellHook = ''
              if [ -d "$PWD/src/busmust" ]; then
                export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              fi
            '';
          };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          package = self.packages.${system}.default;
          integration =
            pkgs.runCommand "busmust-python-can-libusb-check"
              {
                nativeBuildInputs = [ self.packages.${system}.python ];
              }
              ''
                python - <<'PY'
                from importlib.metadata import version
                import busmust
                import can
                import usb.backend.libusb1

                assert version("python-can-busmust") == busmust.__version__
                assert can.interfaces.BACKENDS["busmust"] == ("busmust", "BusMustBus")
                assert usb.backend.libusb1.get_backend() is not None, "libusb backend unavailable"
                PY
                touch "$out"
              '';
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixfmt);
    };
}
