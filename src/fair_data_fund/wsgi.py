"""This module implements the entire HTTP interface."""

import json
import os
import logging
from werkzeug.utils import redirect, send_file
from werkzeug.wrappers import Request, Response
from werkzeug.routing import Map, Rule
from werkzeug.middleware.shared_data import SharedDataMiddleware
from werkzeug.exceptions import HTTPException, NotFound, BadRequest
from jinja2 import Environment, FileSystemLoader
from jinja2.exceptions import TemplateNotFound
from fair_data_fund import database
from fair_data_fund import validator
from fair_data_fund import email_handler
from fair_data_fund.convenience import value_or_none, value_or

## Error handling for loading python3-saml is done in 'ui'.
## So if it fails here, we can safely assume we don't need it.
try:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    from onelogin.saml2.errors import OneLogin_Saml2_Error
except (ImportError, ModuleNotFoundError):
    pass

def R (uri_path, endpoint):  # pylint: disable=invalid-name
    """
    Short-hand for defining a route between a URI and its
    entry-point procedure.
    """
    return Rule (uri_path, endpoint=endpoint)


class WebUserInterfaceServer:
    """This class implements the HTTP interaction for the web user interface."""

    def __init__ (self, address="127.0.0.1", port=8080):

        self.url_map          = Map([
            R("/",                                      self.ui_home),
            R("/login",                                 self.ui_login),
            R("/logout",                                self.ui_logout),
            R("/application/<uuid>",                    self.ui_application_overview),
            R("/application-form",                      self.ui_application_form),
            R("/application-form/<uuid>",               self.ui_application_form),
            R("/application-form/<uuid>/upload-budget", self.ui_upload_budget),
            R("/application-form/<uuid>/submit",        self.ui_submit_application_form),
            R("/review/dashboard",                      self.ui_review_dashboard),
            R("/review/<uuid>",                         self.ui_review_application),
            R("/review/budget/<uuid>",                  self.ui_review_application_budget),
            R("/ranking",                               self.ui_ranking),
            R("/robots.txt",                            self.robots_txt),
            R("/saml/metadata",                         self.saml_metadata),
            R("/saml/login",                            self.ui_login),
        ])
        self.allow_crawlers   = False
        self.maintenance_mode = False
        self.base_url         = f"http://{address}:{port}"
        self.cookie_key       = "ssi_session"
        self.db               = database.SparqlInterface()  # pylint: disable=invalid-name
        self.email            = email_handler.EmailInterface()
        self.repositories     = {}
        self.identity_provider = None
        self.ranking_reviewers = []
        self.saml_config_path    = None
        self.saml_config         = None
        self.saml_attribute_email = "urn:mace:dir:attribute-def:mail"
        self.saml_attribute_first_name = "urn:mace:dir:attribute-def:givenName"
        self.saml_attribute_last_name = "urn:mace:dir:attribute-def:sn"
        self.saml_attribute_common_name = "urn:mace:dir:attribute-def:cn"
        self.saml_attribute_groups = None
        self.saml_attribute_group_prefix = None

        self.automatic_login_email = None
        self.in_production    = False
        self.submissions_open = True
        resources_path        = os.path.dirname(__file__)
        self.jinja            = Environment(loader = FileSystemLoader([
            os.path.join(resources_path, "resources", "html_templates"),
            "/"
        ]), autoescape=True)
        self.static_roots     = {
            "/robots.txt": os.path.join(resources_path, "resources", "robots.txt"),
            "/static":     os.path.join(resources_path, "resources", "static")
        }
        self.log_access       = self.log_access_directly
        self.log              = logging.getLogger(__name__)
        self.wsgi             = SharedDataMiddleware(self.__respond, self.static_roots)
        self.using_uwsgi      = False

        logging.getLogger('werkzeug').setLevel(logging.ERROR)

    def __call__ (self, environ, start_response):
        return self.wsgi (environ, start_response)

    def __dispatch_request (self, request):
        adapter = self.url_map.bind_to_environ(request.environ)
        try:
            self.log_access (request)
            if self.maintenance_mode:
                return self.ui_maintenance (request)
            endpoint, values = adapter.match() #  pylint: disable=unpacking-non-sequence
            return endpoint (request, **values)
        except NotFound:
            return self.error_404 (request)
        except BadRequest as error:
            self.log.error ("Received bad request: %s", error)
            return self.error_400 (request, error.description, 400)
        except HTTPException as error:
            self.log.error ("Unknown error in dispatch_request: %s", error)
            return error
        # Broad catch-all to improve logging/debugging of such situations.
        except Exception as error:
            self.log.error ("In request: %s", request.environ)
            raise error

    def __respond (self, environ, start_response):
        request  = Request(environ)
        response = self.__dispatch_request(request)
        return response(environ, start_response)

    def __render_template (self, request, template_name, **context):
        try:
            template   = self.jinja.get_template (template_name)
            parameters = {
                "base_url":     self.base_url,
                "path":         request.path
            }
            return self.response (template.render({ **context, **parameters }),
                                  mimetype='text/html')
        except TemplateNotFound:
            self.log.error ("Jinja2 template not found: '%s'.", template_name)

        return self.error_500 ()

    # REQUEST CHECKERS
    # -------------------------------------------------------------------------

    def accepts_content_type (self, request, content_type, strict=True):
        """Procedure to check whether the client accepts a content type."""
        try:
            acceptable = request.headers['Accept']
            if not acceptable:
                return False

            exact_match  = content_type in acceptable
            if strict:
                return exact_match

            global_match = "*/*" in acceptable
            return global_match or exact_match
        except KeyError:
            return False

    def accepts_html (self, request):
        """Procedure to check whether the client accepts HTML."""
        return self.accepts_content_type (request, "text/html")

    def accepts_plain_text (self, request):
        """Procedure to check whether the client accepts plain text."""
        return (self.accepts_content_type (request, "text/plain") or
                self.accepts_content_type (request, "*/*"))

    def accepts_xml (self, request):
        """Procedure to check whether the client accepts XML."""
        return (self.accepts_content_type (request, "application/xml") or
                self.accepts_content_type (request, "text/xml"))

    def accepts_json (self, request):
        """Procedure to check whether the client accepts JSON."""
        return self.accepts_content_type (request, "application/json", strict=False)

    # ERROR HANDLERS
    # -------------------------------------------------------------------------

    def error_authorization_failed (self, request):
        """Procedure to handle authorization failures."""
        if self.accepts_html (request):
            response = self.__render_template (request, "403.html")
        else:
            response = self.response (json.dumps({
                "message": "Invalid or unknown session token",
                "code":    "InvalidSessionToken"
            }))

        response.status_code = 403
        return response

    def error_400_list (self, request, errors):
        """Procedure to respond with HTTP 400 with a list of error messages."""
        response = None
        if self.accepts_html (request):
            response = self.__render_template (request, "400.html", message=errors)
        else:
            response = self.response (json.dumps(errors))
        response.status_code = 400
        return response

    def error_400 (self, request, message, code):
        """Procedure to respond with HTTP 400 with a single error message."""
        return self.error_400_list (request, {
            "message": message,
            "code":    code
        })

    def error_403 (self, request):
        """Procedure to respond with HTTP 403."""
        response = None
        if self.accepts_html (request):
            response = self.__render_template (request, "403.html")
        else:
            response = self.response (json.dumps({
                "message": "Not allowed."
            }))
        response.status_code = 403
        return response

    def error_404 (self, request):
        """Procedure to respond with HTTP 404."""
        response = None
        if self.accepts_html (request):
            response = self.__render_template (request, "404.html")
        else:
            response = self.response (json.dumps({
                "message": "This resource does not exist."
            }))
        response.status_code = 404
        return response

    def error_405 (self, allowed_methods):
        """Procedure to respond with HTTP 405."""
        response = self.response (f"Acceptable methods: {allowed_methods}",
                                  mimetype="text/plain")
        response.status_code = 405
        return response

    def error_406 (self, allowed_formats):
        """Procedure to respond with HTTP 406."""
        response = self.response (f"Acceptable formats: {allowed_formats}",
                                  mimetype="text/plain")
        response.status_code = 406
        return response

    def error_415 (self, allowed_types):
        """Procedure to respond with HTTP 415."""
        response = self.response (f"Supported Content-Types: {allowed_types}",
                                  mimetype="text/plain")
        response.status_code = 415
        return response

    def error_500 (self):
        """Procedure to respond with HTTP 500."""
        response = self.response ("")
        response.status_code = 500
        return response

    # CONVENIENCE
    # -------------------------------------------------------------------------

    def token_from_cookie (self, request, cookie_key=None):
        """Procedure to gather an access token from a HTTP cookie."""
        if cookie_key is None:
            cookie_key = self.cookie_key
        return value_or_none (request.cookies, cookie_key)

    def token_from_request (self, request):
        """Procedure to get the access token from a HTTP request."""
        try:
            token_string = self.token_from_cookie (request)
            if token_string is None:
                token_string = request.environ["HTTP_AUTHORIZATION"]
            if isinstance(token_string, str) and token_string.startswith("token "):
                token_string = token_string[6:]
            return token_string
        except KeyError:
            return None

    def account_uuid_from_request (self, request):
        """Procedure to the account UUID for a HTTP request."""
        token = self.token_from_request (request)
        account = self.db.account_by_session_token (token)
        if account is None:
            self.log.error ("Attempt to authenticate with %s failed.", token)
            return None
        return value_or_none (account, "uuid")

    def default_error_handling (self, request, methods, content_type):
        """Procedure to handle both method and content type mismatches."""
        if isinstance (methods, str):
            methods = [methods]

        if (request.method not in methods and
            (not ("GET" in methods and request.method == "HEAD"))):
            return self.error_405 (methods)

        if not self.accepts_content_type (request, content_type, strict=False):
            return self.error_406 (content_type)

        return None

    def default_authenticated_error_handling (self, request, methods, content_type):
        """Procedure to handle method and content type mismatches as well authentication."""

        handler = self.default_error_handling (request, methods, content_type)
        if handler is not None:
            return handler

        account_uuid = self.account_uuid_from_request (request)
        if account_uuid is None:
            return self.error_authorization_failed (request)

        return account_uuid

    def default_list_response (self, records, format_function, **parameters):
        """Procedure to respond a list of items."""
        output     = []
        try:
            for record in records:
                output.append(format_function ({ **parameters, **record}))
        except TypeError:
            self.log.error ("%s: A TypeError occurred.", format_function)

        return self.response (json.dumps(output))

    def respond_201 (self):
        """Procedure to respond with HTTP 201."""
        return Response("", 201, {})

    def respond_204 (self):
        """Procedure to respond with HTTP 204."""
        return Response("", 204, {})

    def response (self, content, mimetype='application/json'):
        """Returns a self.response object with some tweaks."""
        return Response(content, mimetype=mimetype)

    def log_access_using_x_forwarded_for (self, request):
        """Log interactions using the X-Forwarded-For header."""
        try:
            self.log.access ("%s requested %s %s.",  # pylint: disable=no-member
                             request.headers["X-Forwarded-For"],
                             request.method,
                             request.full_path)
        except KeyError:
            self.log.error ("Missing X-Forwarded-For header.")

    def log_access_directly (self, request):
        """Log interactions using the 'remote_addr' property."""
        self.log.access ("%s requested %s %s.",  # pylint: disable=no-member
                         request.remote_addr,
                         request.method,
                         request.full_path)


    def __request_to_saml_request (self, request):
        """Turns a werkzeug request into one that python3-saml understands."""

        return {
            ## Always assume HTTPS.  A proxy server may mask it.
            "https":       "on",
            ## Override the internal HTTP host because a proxy server masks the
            ## actual HTTP host used.  Fortunately, we pre-configure the
            ## expected HTTP host in the form of the "base_url".  So we strip
            ## off the protocol prefix.
            "http_host":   self.base_url.split("://")[1],
            "script_name": request.path,
            "get_data":    request.args.copy(),
            "post_data":   request.form.copy()
        }

    def __saml_auth (self, request):
        """Returns an instance of OneLogin_Saml2_Auth."""
        http_fields = self.__request_to_saml_request (request)
        return OneLogin_Saml2_Auth (http_fields, custom_base_path=self.saml_config_path)

    # ENDPOINTS
    # -------------------------------------------------------------------------

    def saml_metadata (self, request):
        """Communicates the service provider metadata for SAML 2.0."""

        if not (self.accepts_content_type (request, "application/samlmetadata+xml", strict=False) or
                self.accepts_xml (request)):
            return self.error_406 ("text/xml")

        if self.identity_provider != "saml":
            return self.error_404 (request)

        saml_auth   = self.__saml_auth (request)
        settings    = saml_auth.get_settings ()
        metadata    = settings.get_sp_metadata ()
        errors      = settings.validate_metadata (metadata)
        if len(errors) == 0:
            return self.response (metadata, mimetype="text/xml")

        self.log.error ("SAML SP Metadata validation failed.")
        self.log.error ("Errors: %s", ", ".join(errors))
        return self.error_500 ()

    def authenticate_using_saml (self, request):
        """Returns a record upon success, None otherwise."""

        http_fields = self.__request_to_saml_request (request)
        saml_auth   = OneLogin_Saml2_Auth (http_fields, custom_base_path=self.saml_config_path)
        try:
            saml_auth.process_response ()
        except OneLogin_Saml2_Error as error:
            if error.code == OneLogin_Saml2_Error.SAML_RESPONSE_NOT_FOUND:
                self.log.error ("Missing SAMLResponse in POST data.")
            else:
                self.log.error ("SAML error %d occured.", error.code)
            return None

        errors = saml_auth.get_errors()
        if errors:
            self.log.error ("Errors in the SAML authentication:")
            self.log.error ("%s", ", ".join(errors))
            return None

        if not saml_auth.is_authenticated():
            self.log.error ("SAML authentication failed.")
            return None

        ## Gather SAML session information.
        session = {}
        session['samlNameId']                = saml_auth.get_nameid()
        session['samlNameIdFormat']          = saml_auth.get_nameid_format()
        session['samlNameIdNameQualifier']   = saml_auth.get_nameid_nq()
        session['samlNameIdSPNameQualifier'] = saml_auth.get_nameid_spnq()
        session['samlSessionIndex']          = saml_auth.get_session_index()

        ## Gather attributes from user.
        record               = {}
        attributes           = saml_auth.get_attributes()
        record["session"]    = session
        try:
            record["email"]      = attributes[self.saml_attribute_email][0]
            record["first_name"] = attributes[self.saml_attribute_first_name][0]
            record["last_name"]  = attributes[self.saml_attribute_last_name][0]
        except (KeyError, IndexError):
            self.log.error ("Didn't receive expected fields in SAMLResponse.")
            self.log.error ("Received attributes: %s", attributes)

        if not record["email"]:
            self.log.error ("Didn't receive required fields in SAMLResponse.")
            self.log.error ("Received attributes: %s", attributes)
            return None

        record["domain"] = record["email"].partition("@")[2]

        return record

    def robots_txt (self, request):  # pylint: disable=unused-argument
        """Implements /robots.txt."""

        output = "User-agent: *\n"
        if self.allow_crawlers:
            output += "Allow: /\n"
        else:
            output += "Disallow: /\n"

        return self.response (output, mimetype="text/plain")

    def ui_home (self, request):  # pylint: disable=unused-argument
        """Implements /."""
        return self.__render_template (request, "home.html",
                                       submissions_open = self.submissions_open)

    def ui_maintenance (self, request):
        """Implements a maintenance page."""

        if self.accepts_html (request):
            return self.__render_template (request, "maintenance.html")

        return self.response (json.dumps({ "status": "maintenance" }))

    def ui_upload_budget (self, request, uuid):
        """Implements /application/<uuid>/upload-budget."""

        handler = self.default_error_handling (request, "POST", "application/json")
        if handler is not None:
            return handler

        if not validator.is_valid_uuid (uuid):
            return self.error_403 (request)

        application = self.db.applications (application_uuid = uuid)
        if application is None:
            return self.error_403 (request)

        content_type = value_or (request.headers, "Content-Type", "")
        if not content_type.startswith ("multipart/form-data"):
            return self.error_415 (["multipart/form-data"])

        boundary = None
        try:
            boundary = content_type.split ("boundary=")[1]
            boundary = boundary.split(";")[0]
        except IndexError:
            self.log.error ("File upload failed due to missing boundary.")
            return self.error_400 (
                request,
                "Missing boundary for multipart/form-data.",
                "MissingBoundary")

        bytes_to_read = request.content_length
        if bytes_to_read is None:
            self.log.error ("File upload failed due to missing Content-Length.")
            return self.error_400 (
                request,
                "Missing Content-Length header.",
                "MissingContentLength")

        input_stream = request.stream

        # Read the boundary, plus '--', plus CR/LF.
        read_ahead_bytes = len(boundary) + 4
        boundary_scan  = input_stream.read (read_ahead_bytes)
        expected_begin = f"--{boundary}\r\n".encode("utf-8")
        expected_end   = f"\r\n--{boundary}--\r\n".encode("utf-8")
        if not boundary_scan == expected_begin:
            self.log.error ("File upload failed due to unexpected read while parsing.")
            self.log.error ("Scanned:  '%s'", boundary_scan)
            self.log.error ("Expected: '%s'", expected_begin)
            return self.error_400 (request,
                                   "Expected stream to start with boundary.",
                                   "MalformedRequest")

        # Read the multi-part headers
        line = None
        header_line_count = 0
        part_headers = ""
        while line != b"\r\n" and header_line_count < 10:
            line = input_stream.readline (4096)
            part_headers += line.decode("utf-8")
            header_line_count += 1

        if "Content-Disposition: form-data" not in part_headers:
            self.log.error ("File upload failed due to missing Content-Disposition.")
            return self.error_400 (request,
                                   "Expected Content-Disposition: form-data",
                                   "MalformedRequest")

        filename = None
        try:
            # Extract filename.
            filename = part_headers.split("filename=")[1].split(";")[0].split("\r\n")[0]
            # Remove quotes from the filename.
            if filename[0] == "\"" and filename[-1] == "\"":
                filename = filename[1:-1]
        except IndexError:
            pass

        headers_len        = len(part_headers.encode('utf-8'))
        computed_file_size = request.content_length - read_ahead_bytes - headers_len - len(expected_end)
        bytes_to_read      = bytes_to_read - read_ahead_bytes - headers_len
        content_to_read    = bytes_to_read - len(expected_end)

        output_filename = f"{self.db.storage}/{uuid}_Budget_Template"
        file_size = 0
        destination_fd = os.open (output_filename, os.O_WRONLY | os.O_CREAT, 0o600)
        is_incomplete = None
        try:
            with open (destination_fd, "wb") as output_stream:
                file_size = 0
                while content_to_read > 4096:
                    chunk = input_stream.read (4096)
                    content_to_read -= 4096
                    file_size += output_stream.write (chunk)

                if content_to_read > 0:
                    chunk = input_stream.read (content_to_read)
                    file_size += output_stream.write (chunk)
                    content_to_read = 0
        except BadRequest:
            is_incomplete = 1
            self.log.error ("Unexpected end of transfer for %s.", output_filename)

        if computed_file_size != file_size:
            is_incomplete = 1
            self.log.error ("File sizes mismatch: Expected %d, but received %d.",
                            computed_file_size, file_size)

        bytes_to_read -= file_size
        if bytes_to_read != len(expected_end):
            is_incomplete = 1
            self.log.error ("Expected different length after file contents: '%d' != '%d'.",
                            bytes_to_read, len(expected_end))

        if is_incomplete != 1:
            ending = input_stream.read (bytes_to_read)
            if ending != expected_end:
                is_incomplete = 1
                self.log.error ("Expected different end after file contents: '%s' != '%s'.",
                                ending, expected_end)

        self.db.update_application_budget_upload (application_uuid = uuid,
                                                  budget_filename  = filename)
        return self.respond_201 ()

    def __handle_application_form (self, request, uuid, submit=False):
        record     = request.get_json ()
        errors     = []
        data_timing_options = ["decades-ago", "years-ago", "recent", "ongoing"]
        refinement_options  = ["apply-metadata-standards",      "additional-data",
                               "anonymisation", "translation",   "integration",
                               "recovery",     "visualisation", "promotion"]
        linked_publication_options = ["yes", "no"]

        interview_consent    = validator.boolean_value (record, "consent_to_interview",    False, False, error_list=errors)
        checkpoints_consent  = validator.boolean_value (record, "consent_to_checkpoints",  False, False, error_list=errors)
        financial_consent    = validator.boolean_value (record, "consent_to_financial",    False, False, error_list=errors)
        organization_consent = validator.boolean_value (record, "consent_to_organization", False, False, error_list=errors)

        if submit:
            if not checkpoints_consent:
                errors.append({
                    "field_name": "checkpoints_consent",
                    "message": "To obtain funding you must consent to attend three checkpoints with a staff member at 4TU.ResearchData."
                })
            if not financial_consent:
                errors.append({
                    "field_name": "financial_consent",
                    "message": "To obtain funding you must gather the financial information from your institute."
                })
            if not organization_consent:
                errors.append({
                    "field_name": "organization_consent",
                    "message": "To obtain funding you must agree that your organization will receive the requested budget."
                })

        parameters = {
            "application_uuid": uuid,
            "name":          validator.string_value (record, "name",          1, 255,   submit, error_list=errors),
            "pronouns":      validator.string_value (record, "pronouns",      0, 255,   error_list=errors),
            "email":         validator.string_value (record, "email",         0, 512,   submit, error_list=errors),
            "institution":   validator.uuid_value   (record, "institution",   submit,   error_list=errors),
            "faculty":       validator.string_value (record, "faculty",       0, 255,   submit, error_list=errors),
            "department":    validator.string_value (record, "department",    0, 255,   submit, error_list=errors),
            "position":      validator.string_value (record, "position",      0, 255,   submit, error_list=errors),
            "discipline":    validator.string_value (record, "discipline",    0, 255,   submit, error_list=errors),
            "datatype":      validator.string_value (record, "datatype",      0, 255,   submit, error_list=errors),
            "description":   validator.string_value (record, "description",   3, 16384, submit, error_list=errors),
            "size":          validator.string_value (record, "size",          0, 255,   submit, error_list=errors),
            "whodoesit":     validator.string_value (record, "whodoesit",     0, 8192,  submit, error_list=errors),
            "achievement":   validator.string_value (record, "achievement",   0, 8192,  submit, error_list=errors),
            "fair_summary":  validator.string_value (record, "fair_summary",  0, 16384, submit, error_list=errors),
            "findable":      validator.string_value (record, "findable",      0, 16384, submit, error_list=errors),
            "accessible":    validator.string_value (record, "accessible",    0, 16384, submit, error_list=errors),
            "interoperable": validator.string_value (record, "interoperable", 0, 16384, submit, error_list=errors),
            "reusable":      validator.string_value (record, "reusable",      0, 16384, submit, error_list=errors),
            "summary":       validator.string_value (record, "summary",       0, 16384, submit, error_list=errors),
            "promotion":     validator.string_value (record, "promotion",     0, 16384, submit, error_list=errors),
            "linked_publication": validator.options_value (record, "linked_publication", linked_publication_options, submit, error_list=errors),
            "data_timing":   validator.options_value (record, "data_timing", data_timing_options, submit, error_list=errors),
            "refinement":    validator.options_value (record, "refinement", refinement_options, submit, error_list=errors),
            "interview_consent":    interview_consent,
            "checkpoints_consent":  checkpoints_consent,
            "financial_consent":    financial_consent,
            "organization_consent": organization_consent,
            "submitted":     submit
        }

        if errors:
            return self.error_400_list (request, errors)

        if self.db.update_application (**parameters):
            return True

        return False

    def ui_application_form (self, request, uuid=None):
        """Implements /application-form."""

        if not self.submissions_open:
            return self.error_403 (request)

        if request.method in ("GET", "HEAD"):
            if not self.accepts_html (request):
                return self.error_406 ("text/html")

            if uuid is None:
                uuid = self.db.create_application ()
                if uuid is None:
                    return self.error_500 ()
                return redirect (f"/application-form/{uuid}", code=302)

            try:
                application  = self.db.applications (application_uuid = uuid)[0]
                institutions = self.db.institutions ()
                return self.__render_template (request,
                                               "application-form.html",
                                               application  = application,
                                               institutions = institutions)
            except (TypeError, IndexError) as error:
                self.log.info ("Access denied: %s", error)
                return self.error_403 (request)

        if request.method == "PUT":
            if uuid is None:
                uuid = self.db.create_application ()
                if uuid is None:
                    return self.error_500 ()

            handler = self.__handle_application_form (request, uuid)
            if isinstance (handler, Response):
                return Response
            if handler:
                return self.respond_204 ()
            return self.error_500 ()

        return self.error_405 (["GET", "PUT"])

    def ui_application_overview (self, request, uuid):
        """Implements /application/<uuid>."""

        account_uuid = self.account_uuid_from_request (request)
        if account_uuid is None:
            return self.error_authorization_failed (request)

        if account_uuid not in self.ranking_reviewers:
            return self.error_403 (request)

        if uuid is None or not validator.is_valid_uuid (uuid):
            return self.error_404 (request)

        if not self.accepts_html (request):
            return self.error_406 ("text/html")


        try:
            application  = self.db.applications (application_uuid = uuid,
                                                 is_submitted     = True)[0]
            institutions = self.db.institutions ()
            parameters = {
                "application": application,
                "institutions": institutions
            }

            return self.__render_template (request,
                                           "application-overview.html",
                                           **parameters)
        except (TypeError, IndexError):
            return self.error_404 (request)

    def ui_submit_application_form (self, request, uuid=None):
        """Implements /application-form/<uuid>/submit."""

        if not self.submissions_open:
            return self.error_403 (request)

        if request.method in ("GET", "HEAD"):
            if uuid is None or not validator.is_valid_uuid (uuid):
                return self.error_404 (request)

            if not self.accepts_html (request):
                return self.error_406 ("text/html")
            try:
                application  = self.db.applications (application_uuid = uuid,
                                                     is_submitted     = True)[0]
                institutions = self.db.institutions ()
                parameters = {
                    "application": application,
                    "institutions": institutions
                }

                if self.email.is_properly_configured ():
                    email_template = self.jinja.get_template ("application-form-submitted.html")
                    html = email_template.render(**parameters)
                    subject = "We received your application for the 4TU.ResearchData FAIR Data Found."
                    if not self.email.send_email (application["email"], subject, None, html):
                        self.log.error ("Failed to send confirmation e-mail to %s.", application["email"])

                return self.__render_template (request,
                                               "application-form-submitted.html",
                                               **parameters)
            except (TypeError, IndexError):
                return self.error_404 (request)

        if request.method == "PUT":
            handler = self.__handle_application_form (request, uuid, submit=True)
            if isinstance (handler, Response):
                return handler
            if handler:
                if self.accepts_html (request):
                    return redirect (f"/application-form/{uuid}/submit", code=302)
                return self.response (json.dumps({
                    "redirect_to": f"/application-form/{uuid}/submit"
                }))
            return self.error_500 ()

        return self.error_405 (["GET", "PUT"])

    def ui_review_dashboard (self, request):
        """"Implements /review/dashboard."""

        account_uuid = self.account_uuid_from_request (request)
        if account_uuid is None:
            return self.error_authorization_failed (request)

        if request.method in ("GET", "HEAD"):
            if not self.accepts_html (request):
                return self.error_406 ("text/html")

            applications = self.db.applications (account_uuid = account_uuid,
                                                 is_submitted = True)
            return self.__render_template (request,
                                           "review/dashboard.html",
                                           applications = applications)

        return self.error_500 ()

    def ui_review_application (self, request, uuid):
        """Implements /review/<uuid>."""

        account_uuid = self.account_uuid_from_request (request)
        if account_uuid is None:
            return self.error_authorization_failed (request)

        if request.method in ("GET", "HEAD"):
            if not self.accepts_html (request):
                return self.error_406 ("text/html")

            try:
                application = self.db.applications (uuid, account_uuid, True)[0]
                return self.__render_template (request,
                                               "review/evaluate.html",
                                               application = application)
            except IndexError:
                return self.error_404 (request)

        if request.method == "PUT":
            parameters = request.get_json()
            errors = []
            record = {
                "reviewer_uuid":       account_uuid,
                "application_uuid":    uuid,
                "refinement_score":    validator.integer_value (parameters, "refinement",    0, 4,     True,  errors),
                "findable_score":      validator.integer_value (parameters, "findable",      0, 4,     True,  errors),
                "accessible_score":    validator.integer_value (parameters, "accessible",    0, 4,     True,  errors),
                "interoperable_score": validator.integer_value (parameters, "interoperable", 0, 4,     True,  errors),
                "reusable_score":      validator.integer_value (parameters, "reusable",      0, 4,     True,  errors),
                "budget_score":        validator.integer_value (parameters, "budget",        0, 4,     True,  errors),
                "achievement_score":   validator.integer_value (parameters, "achievement",   0, 4,     True,  errors),
                "comments":            validator.string_value  (parameters, "comments",      0, 16384, False, errors)
            }

            if errors:
                return self.error_400_list (request, errors)

            if self.db.insert_evaluation (**record):
                return self.respond_204 ()
            return self.error_500 ()

        return self.error_405 (["GET", "PUT"])

    def ui_review_application_budget (self, request, uuid):
        """Implements /review/budget/<uuid>."""

        account_uuid = self.account_uuid_from_request (request)
        if account_uuid is None:
            return self.error_authorization_failed (request)

        if request.method in ("GET", "HEAD"):
            try:
                application = self.db.applications (uuid, account_uuid, True)[0]
                if "budget_filename" not in application:
                    return self.error_404 (request)

                file_path = f"{self.db.storage}/{uuid}_Budget_Template"
                return send_file (file_path,
                                  request.environ,
                                  "application/octet-stream",
                                  as_attachment=True,
                                  download_name=application["budget_filename"])
            except IndexError:
                return self.error_404 (request)

        return self.error_405 ("GET")

    def ui_ranking (self, request):
        """Implements /ranking."""

        account_uuid = self.account_uuid_from_request (request)
        if account_uuid is None:
            return self.error_authorization_failed (request)

        if account_uuid not in self.ranking_reviewers:
            return self.error_403 (request)

        if request.method in ("GET", "HEAD"):
            if not self.accepts_html (request):
                return self.error_406 ("text/html")

            try:
                ranking = self.db.ranking ()
                if not ranking:
                    return self.__render_template (request, "ranking.html", ranking=[])

                for record in ranking:
                    record["total_score"] = (value_or (record, "budget_score",        0) +
                                             value_or (record, "achievement_score",   0) +
                                             value_or (record, "refinement_score",    0) +
                                             value_or (record, "findable_score",      0) +
                                             value_or (record, "accessible_score",    0) +
                                             value_or (record, "interoperable_score", 0) +
                                             value_or (record, "reusable_score",      0))

                ranking = sorted (ranking, key = lambda x: x["total_score"], reverse=True)
                return self.__render_template (request, "ranking.html", ranking = ranking)
            except IndexError:
                return self.error_404 (request)

    def ui_login (self, request):
        """Implements /login."""

        account_uuid = None
        account      = None

        ## Automatic log in for development purposes only.
        ## --------------------------------------------------------------------
        if self.automatic_login_email is not None and not self.in_production:
            account = self.db.account_by_email (self.automatic_login_email)
            if account is None:
                return self.error_403 (request)
            account_uuid = account["uuid"]
            self.log.access ("Account %s logged in via auto-login.", account_uuid) #  pylint: disable=no-member

        ## SAML 2.0 authentication
        ## --------------------------------------------------------------------
        elif self.identity_provider == "saml":

            ## Initiate the login procedure.
            if request.method == "GET":
                saml_auth   = self.__saml_auth (request)
                redirect_url = saml_auth.login()
                response    = redirect (redirect_url)

                return response

            ## Retrieve signed data from SURFConext via the user.
            if request.method == "POST":
                if not self.accepts_html (request):
                    return self.error_406 ("text/html")

                saml_record = self.authenticate_using_saml (request)
                if saml_record is None:
                    return self.error_403 (request)

                try:
                    if "email" not in saml_record:
                        return self.error_400 (request, "Invalid request", "MissingEmailProperty")

                    account = self.db.account_by_email (saml_record["email"].lower())
                    if account:
                        account_uuid = account["uuid"]
                        self.log.access ("Account %s logged in via SAML.", account_uuid) #  pylint: disable=no-member
                    else:
                        account_uuid = self.db.insert_account (
                            email       = saml_record["email"],
                            first_name  = value_or_none (saml_record, "first_name"),
                            last_name   = value_or_none (saml_record, "last_name"),
                            domain      = value_or_none (saml_record, "domain")
                        )
                        if account_uuid is None:
                            self.log.error ("Creating account for %s failed.", saml_record["email"])
                            return self.error_500()
                        self.log.access ("Account %s created via SAML.", account_uuid) #  pylint: disable=no-member
                except TypeError:
                    pass
        else:
            self.log.error ("Unknown identity provider '%s'", self.identity_provider)
            return self.error_500()

        if account_uuid is not None:
            token, session_uuid = self.db.insert_session (account_uuid, name="Website login")
            if session_uuid is None:
                self.log.error ("Failed to create a session for account %s.", account_uuid)
                return self.error_500 ()

            self.log.access ("Created session %s for account %s.", session_uuid, account_uuid) #  pylint: disable=no-member

            response = redirect ("/review/dashboard", code=302)
            response.set_cookie (key=self.cookie_key, value=token, secure=self.in_production)
            return response

        self.log.error ("Failed to create account during login.")
        return self.error_500 ()

    def ui_logout (self, request):
        """Implements /logout."""
        if not self.accepts_html (request):
            return self.error_406 ("text/html")

        response = redirect ("/", code=302)
        self.db.delete_session (self.token_from_cookie (request))
        response.delete_cookie (key=self.cookie_key)
        return response
