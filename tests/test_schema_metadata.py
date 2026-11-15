from graphql import GraphQLArgument, GraphQLField, GraphQLString, build_schema, print_ast

from app.schema_metadata import argument_metadata, root_types


def test_argument_descriptions_types_all_defaults_and_absence():
    schema=build_schema('''enum Mode { SECRET_ENUM } input Input { secret: String }
        type Query { field(
          "Argument docs" text: String = "schema-secret",
          number: Int = 998877, flag: Boolean = true, mode: Mode = SECRET_ENUM,
          input: Input = {secret: "nested-schema-secret"}, list: [String] = ["list-schema-secret"],
          blank: String = null, empty: [String] = [], missing: String): String }''')
    field=schema.query_type.fields['field']
    args={a.name:a for a in argument_metadata(field)}
    assert args['text'].description=='Argument docs' and args['text'].type=='String'
    assert args['missing'].default_value is None and args['missing'].default_value_availability=='absent'
    assert args['empty'].default_value=='[]' and args['empty'].default_value_availability=='available'
    for name in ('text','number','flag','mode','input','list','blank'):
        assert args[name].default_value_availability=='redacted'
    serialized=' '.join(str(a.default_value) for a in args.values())
    for secret in ('schema-secret','998877','SECRET_ENUM','nested-schema-secret','list-schema-secret'):
        assert secret not in serialized
    # Metadata projection must not mutate the actual schema/default AST used by analysis.
    assert 'schema-secret' in print_ast(field.args['text'].ast_node.default_value)
    assert root_types(schema).model_dump()=={'query':'Query','mutation':None,'subscription':None}


def test_legacy_programmatic_default_and_unrepresentable_value_fail_closed():
    field=GraphQLField(GraphQLString,args={'text':GraphQLArgument(GraphQLString,default_value='legacy-secret')})
    result,=argument_metadata(field)
    assert result.default_value_availability=='redacted' and 'legacy-secret' not in result.default_value
    field=GraphQLField(GraphQLString,args={'text':GraphQLArgument(GraphQLString,default_value=object())})
    result,=argument_metadata(field)
    assert result.default_value is None and result.default_value_availability=='unavailable'
