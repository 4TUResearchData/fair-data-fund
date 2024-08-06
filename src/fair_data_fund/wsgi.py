"""This module implements the entire HTTP interface."""

import json
import os
import logging
import requests
from werkzeug.utils import redirect
from werkzeug.wrappers import Request, Response
from werkzeug.routing import Map, Rule
from werkzeug.middleware.shared_data import SharedDataMiddleware
from werkzeug.exceptions import HTTPException, NotFound, BadRequest
from jinja2 import Environment, FileSystemLoader
from jinja2.exceptions import TemplateNotFound
from fair_data_fund import database
from fair_data_fund import validator
from fair_data_fund import formatter
from fair_data_fund.convenience import value_or_none

# Error handling for loading python3-saml is done in 'ui'.
# So if it fails here, we can safely assume we don't need it.
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
            R("/",                                self.ui_home),
            R("/application-form",                self.ui_application_form),
            R("/application-form/<uuid>",         self.ui_application_form),
            R("/robots.txt",                      self.robots_txt),
        ])
        self.allow_crawlers   = False
        self.maintenance_mode = False
        self.base_url         = f"http://{address}:{port}"
        self.cookie_key       = "ssi_session"
        self.db               = database.SparqlInterface()  # pylint: disable=invalid-name
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
            token      = self.token_from_cookie (request)
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

    def ui_application_form (self, request, uuid=None):
        """Implements /application-form."""

        if request.method in ("GET", "HEAD"):
            if uuid is None:
                uuid = self.db.create_application ()
                if uuid is None:
                    return self.error_500 ()
                return redirect (f"/application-form/{uuid}", code=302)

            institutions = self.db.institutions ()
            if self.accepts_html (request):
                data = {
                    "uuid":          uuid,
                    "name":          "",
                    "pronouns":      "",
                    "institution":   None,
                    "faculty":       "",
                    "department":    "",
                    "position":      "",
                    "discipline":    "",
                    "datatype":      "",
                    "description":   "",
                    "size":          "",
                    "whodoesit":     "",
                    "achievement":   "",
                    "fair_summary":  "",
                    "findable":      "",
                    "accessible":    "",
                    "interoperable": "",
                    "reusable":      "",
                    "summary":       ""
                }
                return self.__render_template (request,
                                               "application-form.html",
                                               application = data,
                                               institutions = institutions)
            return self.error_406 ("text/html")

        
        
