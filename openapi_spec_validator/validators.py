import logging
import string

from jsonschema.validators import RefResolver
from openapi_schema_validator import OAS31Validator, oas31_format_checker

from openapi_spec_validator.exceptions import (
    ParameterDuplicateError, ExtraParametersError, UnresolvableParameterError,
    OpenAPIValidationError, DuplicateOperationIDError,
)
from openapi_spec_validator.decorators import ValidationErrorWrapper
from openapi_spec_validator.managers import ResolverManager

log = logging.getLogger(__name__)

wraps_errors = ValidationErrorWrapper(OpenAPIValidationError)


def is_ref(spec):
    return isinstance(spec, dict) and '$ref' in spec


class Dereferencer(object):

    def __init__(self, spec_resolver):
        self.resolver_manager = ResolverManager(spec_resolver)

    def dereference(self, item):
        log.debug("Dereferencing %s", item)
        if item is None or not is_ref(item):
            return item

        ref = item['$ref']
        with self.resolver_manager.in_scope(item) as resolver:
            with resolver.resolving(ref) as target:
                if is_ref(target):
                    target = self.dereference(target)
                return target


class SpecValidator(object):

    def __init__(self, validator_factory, resolver_handlers):
        self.validator_factory = validator_factory
        self.resolver_handlers = resolver_handlers

    def validate(self, spec, spec_url=''):
        for err in self.iter_errors(spec, spec_url=spec_url):
            raise err

    @wraps_errors
    def iter_errors(self, spec, spec_url=''):
        spec_resolver = self._get_resolver(spec_url, spec)
        dereferencer = self._get_dereferencer(spec_resolver)

        validator = self._get_validator(spec_resolver)
        for err in validator.iter_errors(spec):
            yield err

        paths = spec.get('paths', {})
        for err in self._iter_paths_errors(paths, dereferencer):
            yield err

        components = spec.get('components', {})
        for err in self._iter_components_errors(components, dereferencer):
            yield err

    def _get_resolver(self, base_uri, referrer):
        return RefResolver(
            base_uri, referrer, handlers=self.resolver_handlers)

    def _get_dereferencer(self, spec_resolver):
        return Dereferencer(spec_resolver)

    def _get_validator(self, spec_resolver):
        return self.validator_factory.create(spec_resolver)

    def _iter_paths_errors(self, paths, dereferencer):
        return PathsValidator(dereferencer).iter_errors(paths)

    def _iter_components_errors(self, components, dereferencer):
        return ComponentsValidator(dereferencer).iter_errors(components)


class ComponentsValidator(object):

    def __init__(self, dereferencer):
        self.dereferencer = dereferencer

    @wraps_errors
    def iter_errors(self, components):
        components_deref = self.dereferencer.dereference(components)

        schemas = components_deref.get('schemas', {})
        for err in self._iter_schemas_errors(schemas):
            yield err

    def _iter_schemas_errors(self, schemas):
        return SchemasValidator(self.dereferencer).iter_errors(schemas)


class SchemasValidator(object):

    def __init__(self, dereferencer):
        self.dereferencer = dereferencer

    @wraps_errors
    def iter_errors(self, schemas):
        schemas_deref = self.dereferencer.dereference(schemas)
        for name, schema in schemas_deref.items():
            for err in self._iter_schema_errors(schema):
                yield err

    def _iter_schema_errors(self, schema):
        return SchemaValidator(self.dereferencer).iter_errors(schema)


class SchemaValidator(object):

    def __init__(self, dereferencer):
        self.dereferencer = dereferencer

    @wraps_errors
    def iter_errors(self, schema, require_properties=True):
        schema_deref = self.dereferencer.dereference(schema)
        if not isinstance(schema_deref, dict):
            return

        if 'allOf' in schema_deref:
            for inner_schema in schema_deref['allOf']:
                for err in self.iter_errors(
                    inner_schema,
                    require_properties=False
                ):
                    yield err

        required = schema_deref.get('required', [])
        properties = schema_deref.get('properties', {}).keys()
        extra_properties = list(set(required) - set(properties))
        if extra_properties and require_properties:
            yield ExtraParametersError(
                "Required list has not defined properties: {0}".format(
                    extra_properties
                )
            )

        if 'default' in schema_deref:
            default = schema_deref['default']
            nullable = schema_deref.get('nullable', False)
            if default is not None or nullable is not True:
                for err in self._iter_value_errors(schema_deref, default):
                    yield err

    def _iter_value_errors(self, schema, value):
        return ValueValidator(self.dereferencer).iter_errors(schema, value)


class PathsValidator(object):

    def __init__(self, dereferencer, operation_ids_registry=None):
        self.dereferencer = dereferencer
        self.operation_ids_registry = [] if operation_ids_registry is None \
            else operation_ids_registry

    @wraps_errors
    def iter_errors(self, paths):
        paths_deref = self.dereferencer.dereference(paths)
        for url, path_item in paths_deref.items():
            for err in self._iter_path_errors(url, path_item):
                yield err

    def _iter_path_errors(self, url, path_item):
        return PathValidator(
            self.dereferencer, self.operation_ids_registry).iter_errors(
                url, path_item)


class PathValidator(object):

    def __init__(self, dereferencer, operation_ids_registry=None):
        self.dereferencer = dereferencer
        self.operation_ids_registry = [] if operation_ids_registry is None \
            else operation_ids_registry

    @wraps_errors
    def iter_errors(self, url, path_item):
        path_item_deref = self.dereferencer.dereference(path_item)

        for err in self._iter_path_item_errors(url, path_item_deref):
            yield err

    def _iter_path_item_errors(self, url, path_item):
        return PathItemValidator(
            self.dereferencer, self.operation_ids_registry).iter_errors(
                url, path_item)


class PathItemValidator(object):

    OPERATIONS = [
        'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace',
    ]

    def __init__(self, dereferencer, operation_ids_registry=None):
        self.dereferencer = dereferencer
        self.operation_ids_registry = [] if operation_ids_registry is None \
            else operation_ids_registry

    @wraps_errors
    def iter_errors(self, url, path_item):
        path_item_deref = self.dereferencer.dereference(path_item)

        parameters = path_item_deref.get('parameters', [])
        for err in self._iter_parameters_errors(parameters):
            yield err

        for field_name, operation in path_item.items():
            if field_name not in self.OPERATIONS:
                continue

            for err in self._iter_operation_errors(
                    url, field_name, operation, parameters):
                yield err

    def _iter_operation_errors(self, url, name, operation, path_parameters):
        return OperationValidator(
            self.dereferencer, self.operation_ids_registry).iter_errors(
                url, name, operation, path_parameters)

    def _iter_parameters_errors(self, parameters):
        return ParametersValidator(self.dereferencer).iter_errors(parameters)


class OperationValidator(object):

    def __init__(self, dereferencer, seen_ids=None):
        self.dereferencer = dereferencer
        self.seen_ids = [] if seen_ids is None else seen_ids

    @wraps_errors
    def iter_errors(self, url, name, operation, path_parameters=None):
        path_parameters = path_parameters or []
        operation_deref = self.dereferencer.dereference(operation)

        operation_id = operation_deref.get('operationId')
        if operation_id is not None and operation_id in self.seen_ids:
            yield DuplicateOperationIDError(
                "Operation ID '{0}' for '{1}' in '{2}' is not unique".format(
                    operation_id, name, url)
            )
        self.seen_ids.append(operation_id)

        parameters = operation_deref.get('parameters', [])
        for err in self._iter_parameters_errors(parameters):
            yield err

        all_params = list(set(
            list(self._get_path_param_names(path_parameters)) +
            list(self._get_path_param_names(parameters))
        ))

        for path in self._get_path_params_from_url(url):
            if path not in all_params:
                yield UnresolvableParameterError(
                    "Path parameter '{0}' for '{1}' operation in '{2}' "
                    "was not resolved".format(path, name, url)
                )
        return

    def _get_path_param_names(self, params):
        for param in params:
            param_deref = self.dereferencer.dereference(param)
            if param_deref['in'] == 'path':
                yield param_deref['name']

    def _get_path_params_from_url(self, url):
        formatter = string.Formatter()
        path_params = [item[1] for item in formatter.parse(url)]
        return filter(None, path_params)

    def _iter_parameters_errors(self, parameters):
        return ParametersValidator(self.dereferencer).iter_errors(parameters)


class ParametersValidator(object):

    def __init__(self, dereferencer):
        self.dereferencer = dereferencer

    @wraps_errors
    def iter_errors(self, parameters):
        seen = set()
        for parameter in parameters:
            parameter_deref = self.dereferencer.dereference(parameter)
            for err in self._iter_parameter_errors(parameter_deref):
                yield err

            key = (parameter_deref['name'], parameter_deref['in'])
            if key in seen:
                yield ParameterDuplicateError(
                    "Duplicate parameter `{0}`".format(parameter_deref['name'])
                )
            seen.add(key)

    def _iter_parameter_errors(self, parameter):
        return ParameterValidator(self.dereferencer).iter_errors(parameter)


class ParameterValidator(object):

    def __init__(self, dereferencer):
        self.dereferencer = dereferencer

    @wraps_errors
    def iter_errors(self, parameter):
        if 'schema' in parameter:
            schema = parameter['schema']
            schema_deref = self.dereferencer.dereference(schema)
            for err in self._iter_schema_errors(schema_deref):
                yield err

        if 'default' in parameter:
            # only possible in swagger 2.0
            default = parameter['default']
            if default is not None:
                for err in self._iter_value_errors(parameter, default):
                    yield err

    def _iter_value_errors(self, schema, value):
        return ValueValidator(self.dereferencer).iter_errors(schema, value)

    def _iter_schema_errors(self, schema):
        return SchemaValidator(self.dereferencer).iter_errors(schema)


class ValueValidator(object):

    def __init__(self, dereferencer):
        self.dereferencer = dereferencer

    @wraps_errors
    def iter_errors(self, schema, value):
        validator = OAS31Validator(
            schema,
            resolver=self.dereferencer.resolver_manager.resolver,
            format_checker=oas31_format_checker,
        )
        for err in validator.iter_errors(value):
            yield err
