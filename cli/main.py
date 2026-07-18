"""network-tester CLI entry point (argparse `run` and `status` subcommands)."""

import sys


def main(argv=None):
    """Run the CLI; implemented in the topology fetcher stage."""
    print("network-tester CLI is not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
