"""This module implements the entire HTTP interface."""

import json
import os
import logging
from werkzeug.utils import redirect
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
            R("/application-form",                      self.ui_application_form),
            R("/application-form/<uuid>",               self.ui_application_form),
            R("/application-form/<uuid>/upload-budget", self.ui_upload_budget),
            R("/application-form/<uuid>/submit",        self.ui_submit_application_form),
            R("/robots.txt",                            self.robots_txt),
        ])
        self.allow_crawlers   = False
        self.maintenance_mode = False
        self.base_url         = f"http://{address}:{port}"
        self.cookie_key       = "ssi_session"
        self.db               = database.SparqlInterface()  # pylint: disable=invalid-name
        self.email            = email_handler.EmailInterface()
        self.repositories     = {}
        self.identity_provider = None

        self.automatic_login_email = None
        self.in_production    = False

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

    # ENDPOINTS
    # -------------------------------------------------------------------------

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
        return self.__render_template (request, "home.html")

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

    def ui_submit_application_form (self, request, uuid=None):
        """Implements /application-form/<uuid>/submit."""

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
