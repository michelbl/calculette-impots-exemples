#!/usr/bin/env python3
# -*- coding: utf-8 -*-


"""
Transpile (roughly means convert) a JSON AST file to Python source code.
"""


from operator import itemgetter
from string import Template
import argparse
import copy
import glob
import itertools
import json
import logging
import os
import pkg_resources
import pprint
import sys
import textwrap

from toolz.curried import concat, concatv, filter, first, map, mapcat, pipe, sorted, valmap

from calculateur_impots import core, formulas_helpers, python_source_visitors


# Globals


args = None
script_name = os.path.splitext(os.path.basename(__file__))[0]
log = logging.getLogger(script_name)

script_dir_path = os.path.dirname(os.path.abspath(__file__))
generated_dir_path = os.path.abspath(os.path.join(script_dir_path, '..', 'generated'))


# Functions to write source code files


def as_lines(sequence):
    return ''.join(concat(zip(sequence, itertools.repeat('\n'))))


def write_source_file(file_name, source):
    header = """\
# -*- coding: utf-8 -*-
# flake8: noqa
# WARNING: This file is automatically generated by a script. No not modify it by hand!
"""
    file_path = os.path.join(generated_dir_path, file_name)
    with open(file_path, 'w') as output_file:
        output_file.write(as_lines((header, source)))
    log.info('Output file "{}" written with success'.format(file_path))


# Functions to read JSON data files


def iter_ast_json_file_names(filenames, excluded_filenames=None):
    json_file_paths = pipe(
        filenames,
        mapcat(lambda pathname: glob.iglob(os.path.join(args.json_dir, 'ast', pathname))),
        filter(lambda file_path: excluded_filenames is None or os.path.basename(file_path) not in excluded_filenames),
        sorted,
        )
    for json_file_path in json_file_paths:
        json_file_name = os.path.basename(json_file_path)
        file_name_head = os.path.splitext(json_file_name)[0]
        yield json_file_name


def load_dependencies_by_formula_name():
    m_language_parser_dir_path = pkg_resources.get_distribution('m_language_parser').location
    variables_dependencies_file_path = os.path.join(m_language_parser_dir_path, 'json', 'data',
                                                    'formulas_dependencies.json')
    with open(variables_dependencies_file_path) as variables_dependencies_file:
        variables_dependencies_str = variables_dependencies_file.read()
    dependencies_by_formula_name = json.loads(variables_dependencies_str)
    return dependencies_by_formula_name


def load_regles_file(json_file_name):
    return pipe(
        read_ast_json_file(json_file_name),
        filter(lambda node: 'batch' in node['applications']),
        mapcat(python_source_visitors.visit_node),
        )


def load_verifs_file(json_file_name):
    return pipe(
        read_ast_json_file(json_file_name),
        filter(lambda node: 'batch' in node['applications']),
        map(python_source_visitors.visit_node),
        )


def read_ast_json_file(json_file_name):
    nodes = read_json_file(os.path.join('ast', json_file_name))
    assert isinstance(nodes, list)
    return nodes


def read_json_file(json_file_name):
    json_file_path = os.path.abspath(os.path.join(args.json_dir, json_file_name))
    log.info('Loading "{}"...'.format(json_file_path))
    with open(json_file_path) as json_file:
        json_str = json_file.read()
    return json.loads(json_str)


# Main


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--debug', action='store_true', default=False, help='Display debug messages')
    parser.add_argument('-v', '--verbose', action='store_true', default=False, help='Increase output verbosity')
    parser.add_argument('json_dir', help='Directory containing the JSON AST and data files')
    global args
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else (logging.INFO if args.verbose else logging.WARNING),
        stream=sys.stdout,
        )

    if not os.path.isdir(generated_dir_path):
        os.mkdir(generated_dir_path)

    # Transpile constants

    constants_file_name = os.path.join('data', 'constants.json')
    constants = read_json_file(json_file_name=constants_file_name)
    constants_source = pipe(
        constants.items(),
        sorted,
        map(lambda item: '{} = {}'.format(*item)),
        as_lines,
        )
    write_source_file(
        file_name='constants.py',
        source=constants_source,
        )

    # Transpile variables definitions

    variables_definitions_file_name = os.path.join('data', 'variables_definitions.json')
    definition_by_variable_name = read_json_file(json_file_name=variables_definitions_file_name)
    write_source_file(
        file_name='variables_definitions.py',
        source='definition_by_variable_name = {}\n'.format(pprint.pformat(definition_by_variable_name, width=120)),
        )

    # Transpile verification functions

    verif_sources = list(
        mapcat(load_verifs_file, iter_ast_json_file_names(filenames=['coc*.json', 'coi*.json']))
        )
    verifs_source = Template("""\
from ..formulas_helpers import arr, cached, inf, interval, null, positif, positif_ou_nul, present, somme
from .constants import *  # noqa


def get_errors(formulas, saisie_variables):
    errors = []

$verifs
    return errors or None
""").substitute(verifs=textwrap.indent('\n'.join(verif_sources), prefix=4 * ' '))
    write_source_file(
        file_name='verifs.py',
        source=verifs_source,
        )

    # Transpile formulas

    source_by_formula_name = dict(list(mapcat(
        load_regles_file,
        iter_ast_json_file_names(filenames=['chap-*.json', 'res-ser*.json']),
        )))

    def get_formula_source(formula_name):
        sanitized_name = core.sanitized_variable_name(formula_name)
        return source_by_formula_name[sanitized_name] \
            if sanitized_name in source_by_formula_name \
        else python_source_visitors.make_formula_source(
            cached=False,
            formula_name=formula_name,
            expression='0',
            )

    def should_be_in_formulas_file(variable_name):
        return not core.is_saisie_variable(variable_name) and not core.is_constant(variable_name)

    dependencies_by_formula_name = load_dependencies_by_formula_name()
    formula_names = pipe(
        concatv(
            dependencies_by_formula_name.keys(),
            concat(dependencies_by_formula_name.values()),
            definition_by_variable_name.keys(),
            ),
        set,
        filter(should_be_in_formulas_file),
        sorted,
        )
    write_source_file(
        file_name='formulas.py',
        source=Template("""\
import inspect

from ..formulas_helpers import arr, cached, inf, interval, null, positif, positif_ou_nul, present, somme
from .constants import *  # noqa


def get_formulas(cache, saisie_variables):
    formulas = {}

$formulas
    return {
        key: val
        for key, val in locals().items()
        if inspect.isfunction(val)
        }
""").substitute(
            formulas=textwrap.indent(
                '\n'.join(map(get_formula_source, formula_names)),
                prefix=4 * ' ',
                ),
            ),
        )

    return 0


if __name__ == '__main__':
    sys.exit(main())
