"""This module provides the communication with the SPARQL endpoint."""

import os
import logging
from datetime import datetime
from urllib.error import URLError, HTTPError
from rdflib import Dataset, Graph, Literal, RDF, RDFS, XSD, URIRef
from rdflib.plugins.stores import sparqlstore
from rdflib.store import CORRUPTED_STORE, NO_STORE
from jinja2 import Environment, FileSystemLoader
from fair_data_fund import cache, rdf
from fair_data_fund.convenience import epoch_to_human_readable

class SparqlInterface:
    """This class reads and writes data from a SPARQL endpoint."""

    def __init__ (self):

        self.endpoint     = "http://127.0.0.1:8890/sparql"
        self.update_endpoint = None
        self.state_graph  = "default://graph"
        self.log          = logging.getLogger(__name__)
        self.cache        = cache.CacheLayer(None)

        sparql_templates_path = os.path.join(os.path.dirname(__file__),
                                             "resources",
                                             "sparql_templates")
        self.jinja        = Environment(loader = FileSystemLoader(sparql_templates_path),
                                        autoescape = True)
        self.storage      = None
        self.sparql       = None
        self.sparql_is_up = False
        self.enable_query_audit_log = True
        self.store        = None

    # SPARQL INTERACTION BITS
    # -------------------------------------------------------------------------

    def setup_sparql_endpoint (self):
        """Procedure to be called after setting the 'endpoint' members."""

        # BerkeleyDB as local RDF store.
        if (isinstance (self.endpoint, str) and self.endpoint.startswith("bdb://")):
            directory = self.endpoint[6:]
            self.sparql = Dataset("BerkeleyDB")
            self.sparql.open (directory, create=True)
            if not isinstance (self.sparql, Dataset):
                if self.sparql == CORRUPTED_STORE:
                    self.log.error ("'%s' is corrupted.", directory)
                elif self.sparql == NO_STORE:
                    self.log.error ("'%s' is not a BerkeleyDB store.", directory)
                else:
                    self.log.error ("Loading '%s' returned %s.", directory, self.sparql)
                return None
            self.log.info ("Using BerkeleyDB RDF store.")

        # External SPARQL endpoints, like Virtuoso.
        else:
            if self.update_endpoint is None:
                self.update_endpoint = self.endpoint

            self.store = sparqlstore.SPARQLUpdateStore(
                # Avoid rdflib from wrapping in a blank-node graph by setting
                # context_aware to False.
                context_aware   = False,
                query_endpoint  = self.endpoint,
                update_endpoint = self.update_endpoint,
                returnFormat    = "json",
                method          = "POST")
            # Set bind_namespaces so rdflib does not inject PREFIXes.
            self.sparql  = Graph(store = self.store, bind_namespaces = "none")
            self.log.info ("Using external RDF store.")

        self.sparql_is_up = True
        return None

    def __log_query (self, query, prefix="Query"):
        self.log.info ("%s:\n---\n%s\n---", prefix, query)

    def __query_from_template (self, name, args=None):
        template   = self.jinja.get_template (f"{name}.sparql")
        parameters = { "state_graph": self.state_graph }
        if args is None:
            args = {}

        return template.render ({ **args, **parameters })

    def __normalize_binding (self, row):
        output = {}
        for name in row.keys():
            if isinstance(row[name], Literal):
                xsd_type = row[name].datatype
                if xsd_type == XSD.integer:
                    if str(name).endswith("_date"):
                        output[str(name)] = epoch_to_human_readable (int(row[name]))
                    else:
                        output[str(name)] = int(float(row[name]))
                elif xsd_type == XSD.decimal:
                    output[str(name)] = int(float(row[name]))
                elif xsd_type == XSD.boolean:
                    try:
                        output[str(name)] = bool(int(row[name]))
                    except ValueError:
                        output[str(name)] = str(row[name]).lower() == "true"
                elif xsd_type == XSD.dateTime:
                    self.log.warning ("Using xsd:dateTime is deprecated.")
                    time_value = row[name].partition(".")[0]
                    if time_value[-1] == 'Z':
                        time_value = time_value[:-1]
                    if time_value.endswith("+00:00"):
                        time_value = time_value[:-6]
                    output[str(name)] = time_value
                elif xsd_type == XSD.date:
                    output[str(name)] = row[name]
                elif xsd_type == XSD.string:
                    if row[name] == "NULL":
                        output[str(name)] = None
                    else:
                        output[str(name)] = str(row[name])
                # bindings that were produced with BIND() on Virtuoso
                # have no XSD type.
                elif xsd_type is None:
                    output[str(name)] = str(row[name])
            elif row[name] is None:
                output[str(name)] = None
            else:
                output[str(name)] = str(row[name])

        return output

    def __run_query (self, query, cache_key_string=None, prefix=None, retries=5):

        cache_key = None
        if cache_key_string is not None:
            cache_key = self.cache.make_key (cache_key_string)
            cached    = self.cache.cached_value(prefix, cache_key)
            if cached is not None:
                return cached

        results = []
        try:
            execution_type, query_type = rdf.query_type (query)
            if execution_type == "update":
                self.sparql.update (query)
                # Upon failure, an exception is thrown.
                if self.enable_query_audit_log:
                    self.__log_query (query, "Query Audit Log")
                results = True
            elif execution_type == "gather":
                query_results = self.sparql.query(query)
                # ASK queries only return a boolean.
                if query_type == "ASK":
                    results = query_results.askAnswer
                elif isinstance(query_results, tuple):
                    self.log.error ("Error executing query (%s): %s",
                                    query_results[0], query_results[1])
                    self.__log_query (query)
                    return []
                else:
                    results = list(map(self.__normalize_binding,
                                       query_results.bindings))
            else:
                self.log.error ("Invalid query (%s, %s)", execution_type, query_type)
                self.__log_query (query)
                return []

            if cache_key_string is not None:
                self.cache.cache_value (prefix, cache_key, results, query)

            if not self.sparql_is_up:
                self.log.info ("Connection to the SPARQL endpoint seems up again.")
                self.sparql_is_up = True

        except HTTPError as error:
            if error.code == 400:
                self.log.error ("Badly formed SPARQL query:")
                self.__log_query (query)
            if error.code == 404:
                if self.sparql_is_up:
                    self.log.error ("Endpoint seems not to exist (anymore).")
                    self.sparql_is_up = False
            if error.code == 401:
                if self.sparql_is_up:
                    self.log.error ("Endpoint seems to require authentication.")
                    self.sparql_is_up = False
            if error.code == 503:
                if retries > 0:
                    self.log.warning ("Retrying SPARQL request due to service unavailability (%s)",
                                      retries)
                    return self.__run_query (query, cache_key_string=cache_key_string,
                                             prefix=prefix, retries=(retries - 1)) # pylint: disable=superfluous-parens

                self.log.warning ("Giving up on retrying SPARQL request.")

            self.log.error ("SPARQL endpoint returned %d:\n---\n%s\n---",
                            error.code, error.reason)
            return []
        except URLError:
            if self.sparql_is_up:
                self.log.error ("Connection to the SPARQL endpoint seems down.")
                self.sparql_is_up = False
                return []
        except AttributeError as error:
            self.log.error ("SPARQL query failed.")
            self.log.error ("Exception: %s", error)
            self.__log_query (query)
        except Exception as error:  # pylint: disable=broad-exception-caught
            self.log.error ("SPARQL query failed.")
            self.log.error ("Exception: %s: %s", type(error), error)
            self.__log_query (query)
            return []

        return results

    def __insert_query_for_graph (self, graph):
        if self.enable_query_audit_log:
            query = rdf.insert_query (self.state_graph, graph)
            self.__log_query (query, "Query Audit Log")
            return query
        return rdf.insert_query (self.state_graph, graph)

    def add_triples_from_graph (self, graph):
        """Inserts triples from GRAPH into the state graph."""

        # There's an upper limit to how many triples one can add in a single
        # INSERT query.  To stay on the safe side, we create batches of 250
        # triplets per INSERT query.

        counter             = 0
        processing_complete = True
        insertable_graph    = Graph()

        for subject, predicate, noun in graph:
            counter += 1
            insertable_graph.add ((subject, predicate, noun))
            if counter >= 250:
                query = self.__insert_query_for_graph (insertable_graph)
                if not self.__run_query (query):
                    processing_complete = False
                    break

                # Reset the graph by creating a new one.
                insertable_graph = Graph()
                counter = 0

        query = self.__insert_query_for_graph (insertable_graph)
        if not self.__run_query (query):
            processing_complete = False

        if processing_complete:
            return True

        self.log.error ("Inserting triples from a graph failed.")
        self.__log_query (query)

        return False

    def state_graph_is_initialized (self):
        """"Returns True of the state-graph is already initialized."""
        query = ((f'ASK {{ GRAPH <{self.state_graph}> {{ <this> '
                  f'<{rdf.FDF["initialized"]}> "true"^^<{XSD.boolean}> }} }}'))
        return self.__run_query (query)

    def initialize_database (self):
        """Procedure to initialize the database."""

        # Do not re-initialize.
        if self.state_graph_is_initialized ():
            self.log.info ("Skipping re-initialization of the state-graph.")
            return True

        graph = Graph ()
        delft_uri = rdf.unique_node ("institution")
        rdf.add (graph, delft_uri, RDF.type, rdf.FDF["Institution"], "uri")
        rdf.add (graph, delft_uri, RDFS.label, "Delft University of Technology", XSD.string)

        twente_uri = rdf.unique_node ("institution")
        rdf.add (graph, twente_uri, RDF.type, rdf.FDF["Institution"], "uri")
        rdf.add (graph, twente_uri, RDFS.label, "University of Twente", XSD.string)

        wageningen_uri = rdf.unique_node ("institution")
        rdf.add (graph, wageningen_uri, RDF.type, rdf.FDF["Institution"], "uri")
        rdf.add (graph, wageningen_uri, RDFS.label, "Wageningen University & Research", XSD.string)

        eindhoven_uri = rdf.unique_node ("institution")
        rdf.add (graph, eindhoven_uri, RDF.type, rdf.FDF["Institution"], "uri")
        rdf.add (graph, eindhoven_uri, RDFS.label, "Eindhoven University of Technology", XSD.string)

        rdf.add (graph, URIRef("this"), rdf.FDF["initialized"], True, XSD.boolean)

        query = self.__insert_query_for_graph (graph)
        result = self.__run_query (query)
        return bool(result)

    def institutions (self):
        """Returns a list of institutions."""
        query = self.__query_from_template ("institutions")
        self.__log_query (query)
        return self.__run_query (query)

    def applications (self, application_uuid=None, is_submitted=False):
        """Returns a list of application records."""
        query = self.__query_from_template ("applications", {
            "uuid": application_uuid,
            "is_submitted": is_submitted
        })
        self.__log_query (query)
        return self.__run_query (query)

    def create_application (self):
        """Creates an application entry and returns a unique UUID."""

        graph = Graph()
        uri = rdf.unique_node ("application")

        graph.add ((uri, RDF.type, rdf.FDF["Application"]))
        current_epoch = int(datetime.now().timestamp())
        rdf.add (graph, uri, rdf.FDF["created_date"],     current_epoch, XSD.integer)
        rdf.add (graph, uri, rdf.FDF["modified_date"],    current_epoch, XSD.integer)

        if not self.add_triples_from_graph (graph):
            return None

        return rdf.uri_to_uuid (uri)

    def update_application (self, application_uuid, name=None, pronouns=None,
                            institution=None, faculty=None, department=None,
                            position=None, discipline=None, datatype=None,
                            description=None, size=None, whodoesit=None,
                            achievement=None, fair_summary=None, findable=None,
                            accessible=None, interoperable=None, reusable=None,
                            summary=None, data_timing=None, refinement=None,
                            promotion=None, linked_publication=None,
                            submitted=False, interview_consent=None,
                            checkpoints_consent=None, financial_consent=None,
                            organization_consent=None):
        """
        Returns True when the application identified by UUID has been updated,
        False otherwise.
        """
        current_epoch = int(datetime.now().timestamp())
        query = self.__query_from_template ("update_application", {
            "uuid"          : application_uuid,
            "name"          : rdf.escape_string_value (name),
            "pronouns"      : rdf.escape_string_value (pronouns),
            "institution"   : rdf.escape_string_value (institution),
            "faculty"       : rdf.escape_string_value (faculty),
            "department"    : rdf.escape_string_value (department),
            "position"      : rdf.escape_string_value (position),
            "discipline"    : rdf.escape_string_value (discipline),
            "datatype"      : rdf.escape_string_value (datatype),
            "description"   : rdf.escape_string_value (description),
            "size"          : rdf.escape_string_value (size),
            "whodoesit"     : rdf.escape_string_value (whodoesit),
            "achievement"   : rdf.escape_string_value (achievement),
            "fair_summary"  : rdf.escape_string_value (fair_summary),
            "findable"      : rdf.escape_string_value (findable),
            "accessible"    : rdf.escape_string_value (accessible),
            "interoperable" : rdf.escape_string_value (interoperable),
            "reusable"      : rdf.escape_string_value (reusable),
            "summary"       : rdf.escape_string_value (summary),
            "promotion"     : rdf.escape_string_value (promotion),
            "linked_publication" : rdf.escape_string_value (linked_publication),
            "data_timing"   : rdf.escape_string_value (data_timing),
            "refinement"    : rdf.escape_string_value (refinement),
            "interview_consent" : rdf.escape_boolean_value (interview_consent),
            "checkpoints_consent" : rdf.escape_boolean_value (checkpoints_consent),
            "financial_consent" : rdf.escape_boolean_value (financial_consent),
            "organization_consent" : rdf.escape_boolean_value (organization_consent),
            "submitted"     : submitted,
            "modified_date" : current_epoch
        })
        return self.__run_query (query)
