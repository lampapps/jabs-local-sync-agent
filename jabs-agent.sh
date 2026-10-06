#!/bin/bash

#################################################
# JABS Local Sync Agent Standalone Launcher
#
# This script handles setup, validation, and running
# of the JABS Local Sync Agent with proper environment
# management.
#
# Usage:
#   jabs-agent.sh {setup|logs|reset|check|check-deep|help}
#################################################

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present, so an RSYNC_BIN override is honored by check_rsync()
ENV_FILE="$SCRIPT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Agent configuration
VENV_PATH="$SCRIPT_DIR/venv"
PYTHON_VENV="$VENV_PATH/bin/python"
RUN_SCRIPT="$SCRIPT_DIR/scheduler.py"
CLI_SCRIPT="$SCRIPT_DIR/backup.py"
LOG_FILE="$SCRIPT_DIR/data/logs/sync.log"

# Color output (ANSI-C quoting so these are raw escape bytes, not literal
# backslash text — needed since show_help's heredoc uses `cat`, not `echo -e`)
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
DIM=$'\033[2m'
NC=$'\033[0m' # No Color

# Helper functions
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_header() {
    echo -e "${BLUE}[JABS Local Sync Agent]${NC} $1"
}

print_section() {
    echo -e "${CYAN}[SECTION]${NC} $1"
}

print_success() {
    echo -e " ${GREEN}✓${NC} $1"
}

# Check Python version
check_python() {
    if command -v python3 &>/dev/null; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
        PYTHON_OK=$(python3 -c 'import sys; print(sys.version_info >= (3,8))')
        if [ "$PYTHON_OK" = "True" ]; then
            print_success "Python 3.8+ found: $PYTHON_VERSION"
            return 0
        else
            print_error "Python version $PYTHON_VERSION found, but 3.8+ is required."
            return 1
        fi
    else
        print_error "Python3 not found."
        return 1
    fi
}

# Check rsync binary (honors an RSYNC_BIN override from .env, e.g. an
# absolute path for a non-PATH install; otherwise looks up 'rsync' on PATH)
check_rsync() {
    local rsync_bin="${RSYNC_BIN:-rsync}"
    [[ -z "$rsync_bin" ]] && rsync_bin="rsync"
    if command -v "$rsync_bin" &>/dev/null; then
        local rsync_version
        rsync_version="$("$rsync_bin" --version 2>/dev/null | head -n 1)"
        print_success "rsync found ($rsync_bin): $rsync_version"
        return 0
    else
        print_error "rsync binary not found: $rsync_bin"
        print_error "Install it via your package manager, e.g. 'apt install rsync'."
        print_error "If it's installed somewhere not on PATH, set RSYNC_BIN=/full/path/to/rsync in .env"
        return 1
    fi
}

# Setup virtual environment
setup_virtual_env() {
    if [ -d "$VENV_PATH" ] && [ -f "$VENV_PATH/bin/python" ]; then
        print_success "Virtual environment already exists."
        return 0
    fi

    print_status "Setting up virtual environment..."
    if python3 -m venv "$VENV_PATH"; then
        print_success "Virtual environment created."
        return 0
    else
        print_error "Failed to create virtual environment."
        return 1
    fi
}

# Install requirements
install_requirements() {
    local req_file="$SCRIPT_DIR/requirements.txt"
    if [ ! -f "$req_file" ]; then
        print_error "requirements.txt not found at $req_file"
        return 1
    fi

    if "$PYTHON_VENV" -c "import croniter, portalocker" &>/dev/null; then
        print_success "Requirements already installed."
        return 0
    fi

    print_status "Installing requirements..."
    if "$PYTHON_VENV" -m pip install --upgrade pip && "$PYTHON_VENV" -m pip install -r "$req_file"; then
        print_success "Requirements installed."
        return 0
    else
        print_error "Failed to install requirements."
        return 1
    fi
}

# Validate setup
validate_setup() {
    if [[ ! -f "$PYTHON_VENV" ]]; then
        print_error "Virtual environment not found at: $PYTHON_VENV"
        return 1
    fi
    if [[ ! -f "$RUN_SCRIPT" ]]; then
        print_error "Run script not found at: $RUN_SCRIPT"
        return 1
    fi
    if ! "$PYTHON_VENV" -c "import croniter, portalocker" &>/dev/null; then
        print_error "Agent requirements not properly installed."
        return 1
    fi
    print_success "Setup validation complete."
    return 0
}

# Ensure log/lock/data directories exist
ensure_data_dirs() {
    mkdir -p "$SCRIPT_DIR/data/logs" "$SCRIPT_DIR/locks"
}

# Ensure config/global.yaml and a starter config/jobs/example.yaml exist,
# copying from the tracked example/template files. Never overwrites
# existing files.
ensure_config_files() {
    local config_dir="$SCRIPT_DIR/config"
    local jobs_dir="$config_dir/jobs"

    if [[ ! -f "$config_dir/global.yaml" ]]; then
        if [[ -f "$config_dir/global-example.yaml" ]]; then
            cp "$config_dir/global-example.yaml" "$config_dir/global.yaml"
            print_success "Created config/global.yaml from global-example.yaml"
        else
            print_error "config/global-example.yaml not found; cannot create global.yaml"
            return 1
        fi
    else
        print_success "config/global.yaml already exists."
    fi

    if [[ ! -d "$jobs_dir" ]]; then
        mkdir -p "$jobs_dir"
        print_success "Created config/jobs/ directory."

        local job_template="$config_dir/templates/job.yaml"
        if [[ -f "$job_template" ]]; then
            cp "$job_template" "$jobs_dir/example.yaml"
            print_success "Created config/jobs/example.yaml from templates/job.yaml"
        else
            print_warning "config/templates/job.yaml not found; skipped seeding an example job."
        fi
    else
        print_success "config/jobs/ directory already exists."
    fi

    return 0
}

# Ensure .env exists, copying from the tracked .env.example if needed.
# Never overwrites an existing .env.
ensure_env_file() {
    local env_file="$SCRIPT_DIR/.env"
    local env_example="$SCRIPT_DIR/.env.example"

    if [[ -f "$env_file" ]]; then
        print_success ".env already exists."
        return 0
    fi

    if [[ ! -f "$env_example" ]]; then
        print_warning ".env.example not found; skipping .env creation. Create $env_file manually."
        return 0
    fi

    cp "$env_example" "$env_file"
    print_success "Created .env from .env.example — edit it before running the agent."
    return 0
}

# Show logs
show_logs() {
    if [[ -f "$LOG_FILE" ]]; then
        print_status "Showing sync logs (Press Ctrl+C to exit):"
        tail -f "$LOG_FILE"
    else
        print_error "Log file not found: $LOG_FILE"
        return 1
    fi
}

# Setup agent
setup_agent() {
    print_section "JABS Local Sync Agent Setup"

    check_python || return 1
    check_rsync || return 1
    setup_virtual_env || return 1
    install_requirements || return 1

    ensure_data_dirs
    ensure_config_files || return 1
    ensure_env_file

    if validate_setup; then
        print_success "Agent setup complete!"
        echo ""
        echo -e "${BOLD}Next steps:${NC}"
        echo -e "  1. Edit secrets: ${CYAN}$SCRIPT_DIR/.env${NC} (JABS_AGENT_KEY, JABS_DASHBOARD_URL, SMTP)"
        echo -e "  2. Configure: ${CYAN}$SCRIPT_DIR/config/global.yaml${NC} (mirror behavior, email)"
        echo -e "  3. Create jobs: ${CYAN}$SCRIPT_DIR/config/jobs/*.yaml${NC} (start from config/templates/job.yaml)"
        echo "  4. Run a first sync:"
        echo -e "     ${CYAN}$PYTHON_VENV $CLI_SCRIPT --job example --dry-run${NC}"
        echo "  5. Add CRON for the scheduler: crontab -e"
        echo -e "     ${DIM}*/15 * * * * $PYTHON_VENV $RUN_SCRIPT > /dev/null 2>&1${NC}"
        echo -e "  6. Monitor logs: ${CYAN}$0 logs${NC}"
        return 0
    else
        print_error "Agent setup validation failed."
        return 1
    fi
}

# Reset app (clear logs and locks — NEVER touches config/job files or the
# synced data itself)
reset_app() {
    print_section "JABS Local Sync Agent Reset"
    print_warning "This will NOT touch your synced files or config — only local logs/locks."

    read -r -p "Are you sure you want to reset the JABS Local Sync Agent? [y/N] " confirm
    case "$confirm" in
        [yY]|[yY][eE][sS]) ;;
        *)
            print_status "Reset cancelled."
            return 1
            ;;
    esac

    print_status "Clearing logs..."
    if [ -d "$SCRIPT_DIR/data/logs" ]; then
        rm -f "$SCRIPT_DIR/data/logs"/*.log
        print_success "Logs cleared"
    else
        print_status "No logs directory found (skipped)"
    fi

    print_status "Clearing lock files..."
    if [ -d "$SCRIPT_DIR/locks" ]; then
        rm -f "$SCRIPT_DIR/locks"/*.lock
        print_success "Lock files cleared"
    else
        print_status "No locks directory found (skipped)"
    fi

    echo ""
    print_success "Agent reset complete!"
    echo ""
    echo -e "${BOLD}Preserved items:${NC}"
    echo -e "  ${GREEN}✓${NC} Synced source/destination directories (not touched)"
    echo -e "  ${GREEN}✓${NC} Configuration files"
    echo -e "  ${GREEN}✓${NC} Application code"
    echo -e "  ${GREEN}✓${NC} Virtual environment"
    return 0
}

# Print ready-to-copy commands for running the scheduler and each configured
# sync job on this host, using this machine's actual paths (including the
# venv interpreter) so they can be pasted directly into a terminal.
print_copy_paste_commands() {
    echo -e "${BOLD}COPY/PASTE COMMANDS${NC} ${DIM}(this host)${NC}:"
    echo ""
    echo -e "  ${DIM}CLI syntax reference (backup.py):${NC}"
    echo -e "    ${CYAN}$PYTHON_VENV $CLI_SCRIPT --job JOB_NAME [--dry-run]${NC}"
    echo -e "    ${CYAN}$PYTHON_VENV $CLI_SCRIPT --check [--job JOB_NAME]${NC}"
    echo -e "    ${CYAN}$PYTHON_VENV $CLI_SCRIPT --full-check [--job JOB_NAME]${NC}"
    echo ""
    echo -e "      ${YELLOW}--job${NC}         Job name (matches a file in config/jobs/, without .yaml) or a path to a job YAML"
    echo -e "      ${YELLOW}--dry-run${NC}     Pass --dry-run through to rsync (still reports to the dashboard)"
    echo -e "      ${YELLOW}--check${NC}       Quick verify (size/mtime only), no sync; all jobs if --job omitted"
    echo -e "      ${YELLOW}--full-check${NC}  Deep verify that reads file content (slow), no sync; all jobs if --job omitted"
    echo ""
    echo -e "  ${DIM}Run scheduler manually:${NC}"
    echo -e "    ${CYAN}$PYTHON_VENV $RUN_SCRIPT${NC}"
    echo ""
    echo -e "  ${DIM}Integrity checks (quick runs automatically after every sync too):${NC}"
    echo -e "    ${CYAN}$0 check${NC}                     ${DIM}(quick, size/mtime only, all jobs)${NC}"
    echo -e "    ${CYAN}$0 check-deep${NC}                ${DIM}(slow, reads all data, all jobs)${NC}"
    echo -e "    ${CYAN}$0 check-deep --job example${NC}   ${DIM}(slow, reads all data, one job)${NC}"
    echo ""

    local jobs_dir="$SCRIPT_DIR/config/jobs"
    local found=false
    if [[ -d "$jobs_dir" ]]; then
        for job_file in "$jobs_dir"/*.yaml; do
            [[ -e "$job_file" ]] || continue
            local job_name
            job_name="$(basename "$job_file" .yaml)"
            found=true
            echo -e "  ${DIM}Run job '$job_name' (dry run example — drop --dry-run for a real sync):${NC}"
            echo -e "    ${CYAN}$PYTHON_VENV $CLI_SCRIPT --job \"$job_name\" --dry-run${NC}"
            echo ""
        done
    fi

    if ! $found; then
        echo -e "  ${YELLOW}(No job configs found in $jobs_dir yet — create one first, e.g. from config/templates/job.yaml)${NC}"
        echo ""
    fi
}

# Show help
show_help() {
    cat << EOF
${BOLD}JABS Local Sync Agent Launcher${NC}

${BOLD}USAGE:${NC}
  $0 {setup|logs|reset|check|check-deep}
  $0 help

${BOLD}COMMANDS:${NC}
  ${CYAN}setup${NC}        Setup agent environment (venv, requirements, config, .env)
  ${CYAN}logs${NC}         Follow sync logs
  ${CYAN}reset${NC}        Reset app (clear logs/locks only — never synced data or config)
  ${CYAN}check${NC}        Run a quick verify (size/mtime only; same cheap check run after every sync); add --job NAME for one job
  ${CYAN}check-deep${NC}   Run a deep verify that reads file content (slow); add --job NAME for one job
  ${CYAN}help${NC}         Show this help message

${BOLD}DIRECTORIES:${NC}
  Agent:    $SCRIPT_DIR
  Venv:     $VENV_PATH
  Config:   $SCRIPT_DIR/config
  Log file: $LOG_FILE

${BOLD}SETUP:${NC}
  1. Run: ${CYAN}$0 setup${NC}
  2. Edit: $SCRIPT_DIR/.env
  3. Edit: $SCRIPT_DIR/config/global.yaml
  4. Create sync jobs in: $SCRIPT_DIR/config/jobs/
  5. Add CRON job: crontab -e
     ${DIM}*/15 * * * * $PYTHON_VENV $RUN_SCRIPT > /dev/null 2>&1${NC}

${BOLD}EXAMPLES:${NC}
  ${DIM}# Initial setup${NC}
  $0 setup

  ${DIM}# Check logs${NC}
  $0 logs

  ${DIM}# Reset local state (logs/locks only)${NC}
  $0 reset

  ${DIM}# Quick verify, every configured job (metadata only)${NC}
  $0 check

  ${DIM}# Deep verify one job (reads all data; slow)${NC}
  $0 check-deep --job example

EOF
    print_copy_paste_commands
}

# Quick verify (size/mtime only, no sync). Optional --job NAME restricts to
# one job; all configured jobs are checked otherwise. This is the same cheap
# check that already runs automatically after every sync.
run_check() {
    if [[ ! -x "$PYTHON_VENV" ]]; then
        print_error "Venv not found at $VENV_PATH — run '$0 setup' first."
        exit 1
    fi
    "$PYTHON_VENV" "$CLI_SCRIPT" --check "$@"
}

# Deep verify (reads file content; slow, manual-only). Optional --job NAME
# restricts to one job; all configured jobs are checked otherwise.
run_check_deep() {
    if [[ ! -x "$PYTHON_VENV" ]]; then
        print_error "Venv not found at $VENV_PATH — run '$0 setup' first."
        exit 1
    fi
    print_warning "Deep check reads file content on both sides and can be slow / I/O-heavy."
    "$PYTHON_VENV" "$CLI_SCRIPT" --full-check "$@"
}

# Main function
main() {
    local command="${1:-help}"

    case "$command" in
        setup)
            setup_agent
            ;;
        logs)
            show_logs
            ;;
        reset)
            reset_app
            ;;
        check)
            shift
            run_check "$@"
            ;;
        check-deep)
            shift
            run_check_deep "$@"
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "Unknown command: $command"
            show_help
            exit 1
            ;;
    esac
}

# Run main with all arguments
main "$@"
