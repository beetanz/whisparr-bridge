from invoke import task
import sys
from pathlib import Path
SOURCE_DIRS = ["plugins/whisparr-bridge", "tests"]

# Simple color helpers
def color(text, code):
    return f"\033[{code}m{text}\033[0m"

def green(text): return color(text, "32")
def red(text): return color(text, "31")
def yellow(text): return color(text, "33")

def run_cmd(c, cmd, halt_on_fail=True):
    """Run a shell command and optionally stop on failure."""
    print(yellow(f"▶ Running: {cmd}"))
    result = c.run(cmd, warn=True)
    if halt_on_fail and result.exited != 0:
        print(red(f"❌ Command failed: {cmd}"))
        sys.exit(result.exited)
    return result

def run_linters(c, fix: bool = False):
    """Run linters or formatters depending on fix flag."""
    black_cmd = "black" if fix else "black --check"
    isort_cmd = "isort" if fix else "isort --check-only"

    for path in SOURCE_DIRS:
        print(yellow(f"🔹 Processing {path} ({'formatting' if fix else 'linting'})"))
        run_cmd(c, f"{black_cmd} {path}")
        run_cmd(c, f"{isort_cmd} {path}")
        if not fix:
            run_cmd(c, f"pycodestyle {path}")
            run_cmd(c, f"pylint {path}")

@task(help={"fix": "Automatically format code instead of just checking."})
def lint(c, fix: bool = False):
    """Run linters on all source files."""
    run_linters(c, fix=fix)
    print(green("✅ Linting completed successfully"))

@task
def format(c):
    """Format code automatically."""
    run_linters(c, fix=True)
    print(green("✅ Formatting completed successfully"))

@task
def typecheck(c):
    """Run mypy type checks."""
    for path in SOURCE_DIRS:
        run_cmd(c, f"mypy {path}")
    print(green("✅ Type checking completed successfully"))

@task
def test(c):
    """Run tests with coverage."""
    run_cmd(c, "pytest --cov=whisparr_bridge tests")
    print(green("✅ Tests completed successfully"))

@task(help={"fix": "Automatically format code before running other dev tasks."})
def dev(c, fix: bool = False):
    """Run all dev tasks: lint, typecheck, test."""
    if fix:
        lint(c, fix=True)
    else:
        lint(c)
    typecheck(c)
    test(c)
    print(green("🎉 All dev tasks completed successfully!"))
@task
def export_reqs(c):
    """
    Export requirements.txt and requirements-dev.txt from Poetry lock file.
    Compatible with Poetry 2.x.
    """
    # Ensure export plugin is available
    print("🔹 Checking if Poetry export plugin is installed...")
    result = c.run("poetry export --help", warn=True, hide=True)
    if result.exited != 0:
        print("❌ Poetry export command not found. Installing plugin...")
        c.run("poetry self add poetry-plugin-export")

    # Create requirements directory
    requirements_dir = Path("plugins/whisparr-bridge")
    requirements_dir.mkdir(parents=True, exist_ok=True)

    main_reqs = requirements_dir / "requirements.txt"
    dev_reqs = "requirements-dev.txt"

    print("🔹 Exporting main requirements.txt")
    c.run(f'poetry export -f requirements.txt --output "{main_reqs}" --without-hashes')

    print("🔹 Exporting dev requirements-dev.txt")
    c.run(f'poetry export -f requirements.txt --output "{dev_reqs}" --without-hashes --with dev')

    print("✅ requirements.txt and requirements-dev.txt updated successfully")

