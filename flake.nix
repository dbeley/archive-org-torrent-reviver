{
  description = "ia-reviver — revive dead archive.org torrents by populating them from archive.org itself";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        version = "0.1.0";

        # Pure-stdlib Python, so the derivation needs no runtime dependencies at
        # all: nothing to patch, nothing to pin, and `nix run` works offline once
        # the closure is built.
        ia-reviver = pkgs.python3Packages.buildPythonApplication {
          pname = "ia-reviver";
          inherit version;
          pyproject = true;
          src = self;

          build-system = [ pkgs.python3Packages.setuptools ];

          nativeCheckInputs = [
            pkgs.python3Packages.pytestCheckHook
          ];

          # The suite is offline by design (fake qBittorrent + fake archive.org),
          # so it runs fine inside the Nix sandbox.
          pytestFlags = [ "tests" ];

          pythonImportsCheck = [ "ia_reviver" ];

          meta = {
            description = "Populate unseeded archive.org torrents from archive.org itself";
            homepage = "https://github.com/dbeley/archive-org-torrent-reviver";
            license = pkgs.lib.licenses.mit;
            mainProgram = "ia-reviver";
            platforms = pkgs.lib.platforms.unix;
            maintainers = [ ];
          };
        };
      in
      {
        packages = {
          default = ia-reviver;
          inherit ia-reviver;
        };

        apps = {
          default = {
            type = "app";
            program = "${ia-reviver}/bin/ia-reviver";
            meta = ia-reviver.meta;
          };
          ia-reviver = {
            type = "app";
            program = "${ia-reviver}/bin/ia-reviver";
            meta = ia-reviver.meta;
          };
        };

        checks = {
          inherit ia-reviver;
          # An explicit, fast test target. The package build already runs the
          # suite via pytestCheckHook; this exists so `nix flake check` reports
          # test results as their own named check.
          tests =
            pkgs.runCommand "ia-reviver-tests"
              {
                nativeBuildInputs = [
                  pkgs.python3
                  pkgs.python3Packages.pytest
                ];
              }
              ''
                cp -r ${self} source
                chmod -R u+w source
                cd source
                python -m pytest tests -q -p no:cacheprovider
                touch $out
              '';
        };

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            python3Packages.pytest
            python3Packages.setuptools
            ruff
            # A throwaway torrent client, so the API client can be exercised for
            # real locally: scripts/dev-qbittorrent.sh drives it.
            qbittorrent-nox
            curl
            jq
            git
            gh
            nixfmt
            statix
            deadnix
          ];

          shellHook = ''
            echo "ia-reviver dev shell"
            echo ""
            echo "  python3 -m unittest discover -s tests -t .   run the test suite"
            echo "  python3 -m ia_reviver --help                 run from the checkout"
            echo "  ruff check src tests                         lint"
            echo "  scripts/dev-qbittorrent.sh start             throwaway qBittorrent on :18080"
            echo ""
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      }
    );
}
