"""
This module contains the entry point for the program.
"""

import argparse
import signal
import sys
import logging
import os
import importlib.metadata

from defusedxml import ElementTree
from werkzeug.serving import run_simple
from rdflib.plugins.stores import berkeleydb
from fair_data_fund import wsgi
from fair_data_fund.convenience import value_or_none, add_logging_level, index_exists

class ConfigFileNotFound(Exception):
    """Raised when the database is not queryable."""


class DependencyNotAvailable(Exception):
    """Raised when a required software dependency isn't available."""


class MissingConfigurationError(Exception):
    """Raised when a crucial piece of configuration is missing."""


def show_version ():
    """Show the program's version."""

    version = importlib.metadata.version (__package__ or __name__)
    print(f"This is fair-data-fund v{version}")
    sys.exit(0)


def show_help ():
    """Show a GNU-style help message."""
    print ("""This is fair-data-fund.

Available options:
  --help               -h  Show this message.
  --version            -v  Show versioning information.
  --config-file=ARG    -c Load configuration from a file.
  --initialize         -i Populate the RDF store with default triples.\n""")
    sys.exit(0)


def sigint_handler (sig, frame):  # pylint: disable=unused-argument
    """Signal handler for SIGINT and SIGTERM."""
    logger = logging.getLogger(__name__)
    logger.info ("Received shutdown signal.  Goodbye!")
    sys.exit(0)


def config_value (xml_root, path, command_line=None, fallback=None, return_node=False):
    """Procedure to get the value a config item should have at run-time."""

    # Prefer command-line arguments.
    if command_line:
        return command_line

    # Read from the configuration file.
    if xml_root:
        item = xml_root.find(path)
        if item is not None:
            if return_node:
                return item
            return item.text

    # Fall back to the fallback value.
    return fallback


def read_boolean_value (xml_root, path, default_value, logger):
    """Parses a boolean option and sets DESTINATION if the option is present."""
    try:
        parsed = config_value (xml_root, path, None, None)
        if parsed is not None:
            return bool(int(parsed))
    except ValueError:
        logger.error ("Erroneous value for '%s' - assuming '%s'.", path, default_value)

    return default_value


def configure_file_logging (log_file, inside_reload, logger):
    """Procedure to set up logging to a file."""
    is_writeable = False
    log_file     = os.path.abspath (log_file)
    try:
        with open (log_file, "a", encoding = "utf-8"):
            is_writeable = True
    except (PermissionError, FileNotFoundError):
        pass

    if not is_writeable:
        if not inside_reload:
            logger.warning ("Cannot write to '%s'.", log_file)
    else:
        file_handler = logging.FileHandler (log_file, 'a')
        if not inside_reload:
            logger.info ("Writing further messages to '%s'.", log_file)

        formatter    = logging.Formatter('[%(levelname)s] %(asctime)s - %(name)s: %(message)s')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        log          = logging.getLogger()
        for handler in log.handlers[:]:
            log.removeHandler(handler)
        log.addHandler(file_handler)

def read_automatic_login_configuration (server, xml_root):
    """Procedure to parse and set automatic login for development setups."""
    automatic_login_email = config_value (xml_root, "authentication/automatic-login-email")
    if automatic_login_email is not None:
        server.identity_provider = "automatic-login"
        server.automatic_login_email = automatic_login_email

def read_email_configuration (server, xml_root, logger):
    """Procedure to parse and set the email server configuration."""
    email = xml_root.find("email")
    if email:
        try:
            server.email.smtp_port = int(config_value (email, "port"))
            server.email.smtp_server = config_value (email, "server")
            server.email.from_address = config_value (email, "from")
            server.email.smtp_username = config_value (email, "username")
            server.email.smtp_password = config_value (email, "password")
            server.email.subject_prefix = config_value (email, "subject-prefix", None, None)
            server.email.do_starttls = bool(int(config_value (email, "starttls", None, 0)))
        except ValueError:
            logger.error ("Could not configure the email subsystem:")
            logger.error ("The email port should be a numeric value.")

def read_configuration_file (config, server, config_file, logger, config_files):
    """Procedure to parse a configuration file."""

    inside_reload = os.environ.get('WERKZEUG_RUN_MAIN')
    try:
        if config_file is None:
            raise FileNotFoundError

        tree = ElementTree.parse(config_file)
        if config_file is not None:
            config_files.add (config_file)
            if not inside_reload:
                logger.info ("Reading config file: %s", config_file)

        xml_root = tree.getroot()
        if xml_root.tag != "fair-data-fund":
            raise ConfigFileNotFound

        log_file = config_value (xml_root, "log-file", None, None)
        if log_file is not None:
            config["log-file"] = log_file
            configure_file_logging (log_file, inside_reload, logger)

        address = value_or_none (config, "address")
        port    = value_or_none (config, "port")
        config["address"] = config_value (xml_root, "bind-address", address, "127.0.0.1")
        config["port"]    = int(config_value (xml_root, "port", port, 8080))

        use_reloader = value_or_none (config, "use_reloader")
        use_debugger = value_or_none (config, "use_debugger")
        config["use_reloader"]  = config_value (xml_root, "live-reload", use_reloader)
        config["use_debugger"]  = config_value (xml_root, "debug-mode", use_debugger)

        cache_root = xml_root.find ("cache-root")
        if cache_root is not None:
            server.db.cache.storage = cache_root.text
            try:
                clear_on_start = cache_root.attrib.get("clear-on-start")
                config["clear-cache-on-start"] = bool(int(clear_on_start))
            except ValueError:
                logger.warning ("Invalid value for the 'clear-on-start' attribute in 'cache-root'.")
                logger.warning ("Will not clear cache on start; Use '1' to enable, or '0' to disable.")
                config["clear-cache-on-start"] = False
            except TypeError:
                config["clear-cache-on-start"] = False

        production_mode = xml_root.find ("production")
        if production_mode is not None:
            server.in_production = bool(int(production_mode.text))

        enable_query_audit_log = xml_root.find ("enable-query-audit-log")
        if enable_query_audit_log is not None:
            config["transactions_directory"] = enable_query_audit_log.attrib.get("transactions-directory")
            server.db.enable_query_audit_log = read_boolean_value (xml_root, "enable-query-audit-log",
                                                                   True, logger)

        server.allow_crawlers = read_boolean_value (xml_root, "allow-crawlers",
                                                    server.allow_crawlers, logger)

        base_url_fallback = f"http://{config['address']}:{config['port']}"
        if server.base_url:
            base_url_fallback = server.base_url

        server.base_url = config_value (xml_root, "base-url", None, base_url_fallback)

        server.db.storage     = config_value (xml_root, "storage-root", None, server.db.storage)
        server.db.state_graph = config_value (xml_root, "rdf-store/state-graph")

        endpoint = config_value (xml_root, "rdf-store/sparql-uri")
        if endpoint:
            server.db.endpoint = endpoint

        update_endpoint = config_value (xml_root, "rdf-store/sparql-update-uri")
        if update_endpoint:
            server.db.update_endpoint = update_endpoint

        read_automatic_login_configuration (server, xml_root)
        read_email_configuration (server, xml_root, logger)

        for include_element in xml_root.iter('include'):
            include = include_element.text
            if include is None:
                continue

            if not os.path.isabs(include):
                config_dir = os.path.dirname(config_file)
                include    = os.path.join(config_dir, include)

            new_config = read_configuration_file (config, server, include, logger, config_files)
            config = { **config, **new_config }
            return config

    except ConfigFileNotFound:
        if not inside_reload:
            logger.error ("%s does not look like a configuration file for this program.",
                          config_file)
    except ElementTree.ParseError:
        if not inside_reload:
            logger.error ("%s does not contain valid XML.", config_file)
    except FileNotFoundError as error:
        if not inside_reload:
            if config_file is None:
                logger.error ("No configuration file specified.")
            else:
                logger.error ("Could not open '%s'.", config_file)
        raise SystemExit from error

    return config


def main_inner ():
    """The main entry point of the program."""

    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGTERM, sigint_handler)

    logging.basicConfig(format='[%(levelname)s] %(asctime)s - %(name)s: %(message)s',
                        level=logging.INFO)

    logger = logging.getLogger (__name__)
    parser = argparse.ArgumentParser(
        usage    = '\n  %(prog)s ...',
        prog     = 'fair-data-fund',
        add_help = False)

    parser.add_argument('--help',        '-h', action='store_true')
    parser.add_argument('--version',     '-v', action='store_true')
    parser.add_argument('--config-file', '-c', type=str, default=None)
    parser.add_argument('--initialize',  '-i', action='store_true')

    # When using PyInstaller and Nuitka, argv[0] seems to get duplicated.
    # In the case of Nuitka, relative paths are converted to absolute paths.
    # This bit de-duplicates argv[0] in these cases.
    try:
        if os.path.abspath(sys.argv[0]) == os.path.abspath(sys.argv[1]):
            sys.argv = sys.argv[1:]
    except IndexError:
        pass

    arguments = parser.parse_args()
    if arguments.help:
        show_help()
    if arguments.version:
        show_version()
    if not index_exists (sys.argv, 1):
        print("Try --help for usage options.")
        return None
    try:
        config = {}
        server = wsgi.WebUserInterfaceServer ()
        config_files = set()
        config = read_configuration_file (config, server, arguments.config_file, logger, config_files)

        if (isinstance (server.db.endpoint, str) and
            server.db.endpoint.startswith("bdb://") and
            not berkeleydb.has_bsddb):
            logger.error(("Configured a BerkeleyDB database back-end, "
                          "but BerkeleyDB is not installed on the system "
                          "or the 'berkeleydb' Python package is missing."))
            raise DependencyNotAvailable

        server.db.setup_sparql_endpoint ()

        if arguments.initialize:
            logger.info ("Initialization complete.")
            server.db.initialize_database ()
            server.db.sparql.close()
            return None

        run_simple (config["address"], config["port"], server,
                    threaded=True,
                    processes=1,
                    extra_files=list(config_files),
                    use_debugger=config["use_debugger"],
                    use_reloader=config["use_reloader"])

    except (FileNotFoundError, DependencyNotAvailable, MissingConfigurationError):
        pass

    return None


def main ():
    """Wrapper to catch KeyboardInterrupts for main_inner."""
    try:
        add_logging_level ("ACCESS", logging.INFO + 5)
        add_logging_level ("STORE", logging.INFO + 4)
        main_inner ()
    except KeyboardInterrupt:
        sigint_handler (None, None)

    sys.exit(0)
