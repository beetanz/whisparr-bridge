from invoke import task
import sys

SOURCE_DIRS = ["plugins/whisparr-bridge", "tests"]

@task
def lint(c):
    """Run linters on all source files."""
    for path in SOURCE_DIRS:
        c.run(f"black --check {path}")
        c.run(f"isort --check-only {path}")
        c.run(f"pycodestyle {path}")
        c.run(f"pylint {path}")

@task
def format(c):
    """Format code automatically."""
    for path in SOURCE_DIRS:
        c.run(f"black {path}")
        c.run(f"isort {path}")

@task
def typecheck(c):
    """Run mypy type checks."""
    for path in SOURCE_DIRS:
        c.run(f"mypy {path}")

@task
def test(c):
    """Run tests with coverage."""
    c.run("pytest --cov=whisparr_bridge tests")

@task(pre=[lint, typecheck, test])
def dev(c):
    """Run all dev tasks: lint, typecheck, test."""
    print("✅ All dev tasks completed successfully")
