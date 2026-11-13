import pytest
from graphql import GraphQLSyntaxError

from app.schema_parser import parse_sdl

SDL = """
type Query {
    users: [User!]!
    health: String!
}
type User {
    id: ID!
    name: String!
    email: String!
}
"""


def test_schema_fields_and_type_links_are_real():
    parsed = parse_sdl(SDL)
    coordinates = {(field.type_name, field.name) for field in parsed.fields}
    assert coordinates == {
        ("Query", "users"), ("Query", "health"),
        ("User", "id"), ("User", "name"), ("User", "email"),
    }
    users = next(field for field in parsed.fields if field.name == "users")
    assert users.return_type == "[User!]!"
    assert users.target_type_name == "User"
    assert len(parsed.sha256) == 64


def test_hash_is_independent_of_sdl_whitespace():
    first = parse_sdl(SDL)
    second = parse_sdl("type Query{users:[User!]! health:String!} type User{id:ID! name:String! email:String!}")
    assert first.sha256 == second.sha256


def test_invalid_sdl_does_not_generate_fake_schema():
    with pytest.raises(GraphQLSyntaxError):
        parse_sdl("type Query {")
