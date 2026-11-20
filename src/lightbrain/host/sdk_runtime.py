"""Start a pinned SDK from a private in-memory mount. Project outputs stay on disk."""
import runpy
import subprocess
import sys


def main():
    archive, target, script = sys.argv[1:]
    subprocess.run(["mount","--make-rprivate","/"],check=True)
    subprocess.run(["mount","-t","tmpfs","-o","size=1500m,nosuid,nodev","tmpfs",target],check=True)
    # The installer packs the hash-locked worker environment. This file is worker
    # configuration, never a user project artifact or an input accepted from a job.
    subprocess.run(["tar","-xf",archive,"--no-same-owner","--no-same-permissions"],
                   cwd=target, check=True)
    sys.path.insert(0,target)
    runpy.run_path(script,run_name="__main__")


if __name__ == "__main__":
    main()
