from typing import List, Any, TypeVar, Optional, Type

from luqum.parser import parser
from luqum.tree import Range, Term, Item, Word, Phrase, SearchField, AndOperation, OrOperation, Not, Prohibit, \
                       BaseGroup
from luqum.utils import LuceneTreeVisitorV2

from sqlalchemy import and_, or_, not_

from model import db, Object

from .exceptions import FieldNotQueryableException, UnsupportedGrammarException
from .mappings import get_field_mapper


T = TypeVar('T', bound=Term)
# SQLAlchemy doesn't provide typings
Condition = Any


class SQLQueryBuilderContext:
    def __init__(self, queried_type: Optional[Type[Object]] = None):
        self.queried_type = queried_type or Object
        self.field_node = None


class SQLQueryBuilder(LuceneTreeVisitorV2):
    generic_visitor_method_name = 'visit_unsupported'

    # Visitor methods for value nodes

    def visit_term(self, node: T, parents: List[Item], context: SQLQueryBuilderContext) -> T:
        """
        Visitor for Term (Word and Phrase).
        - checks if field is already set
        - performs wildcard mapping and unescaping
        - wildcards are not allowed inside ranges

        Returns mapped node
        """
        if context.field_node is None:
            raise FieldNotQueryableException("You have to specify field, check help for more information")

        is_range_term = isinstance(parents[-1], Range)

        if node.has_wildcard() and is_range_term:
            raise UnsupportedGrammarException("Wildcards are not supported in range queries")
        return node

    def visit_word(self, node: Word, parents: List[Item], context: SQLQueryBuilderContext) -> Word:
        """
        Visitor for Word. Words are non-enquoted Terms.
        """
        return self.visit_term(node, parents, context)

    def visit_phrase(self, node: Phrase, parents: List[Item], context: SQLQueryBuilderContext) -> Phrase:
        """
        Visitor for Phrase. Phrases are enquoted Terms.
        """
        # Strip the " from start and end
        node.value = node.value[1:-1]
        return self.visit_term(node, parents, context)

    def visit_range(self, node: Range, parents: List[Item], context: SQLQueryBuilderContext) -> Range:
        """
        Visitor for Range
        - inclusive [<Term> TO <Term>]
        - exclusive [<Term> TO <Term>]
        """
        node.low = self.visit(node.low, parents + [node], context)
        node.high = self.visit(node.high, parents + [node], context)
        return node

    # Visitor methods for fields

    def visit_search_field(self, node: SearchField, parents: List[Item], context: SQLQueryBuilderContext) -> Condition:
        field_mapper, name_remainder = get_field_mapper(context.queried_type, node.name)

        if field_mapper.field_type is not Object:
            context.queried_type = field_mapper.field_type

        context.field_node = node
        condition = field_mapper.get_condition(
            self.visit(node.expr, parents + [node], context),
            name_remainder
        )
        context.field_node = None

        return condition

    # Visitor methods for operators

    def visit_and_operation(self, node: AndOperation, parents: List[Item], context: SQLQueryBuilderContext) -> Condition:
        return and_(*[
            self.visit(child_node, parents + [node], context)
            for child_node in node.children
        ])

    def visit_or_operation(self, node: OrOperation, parents: List[Item], context: SQLQueryBuilderContext) -> Condition:
        return or_(*[
            self.visit(child_node, parents + [node], context)
            for child_node in node.children
        ])

    def visit_not(self, node: Not, parents: List[Item], context: SQLQueryBuilderContext) -> Condition:
        return not_(
            self.visit(node.a, parents + [node], context)
        )

    def visit_prohibit(self, node: Prohibit, parents: List[Item], context: SQLQueryBuilderContext) -> Condition:
        return not_(
            self.visit(node.a, parents + [node], context)
        )

    # Visitor methods for other elements

    def visit_group(self, node: BaseGroup, parents: List[Item], context: SQLQueryBuilderContext) -> Condition:
        return self.visit(node.expr, parents + [node], context)

    def visit_unsupported(self, node: Item, parents: List[Item], context: SQLQueryBuilderContext):
        raise UnsupportedGrammarException(
            f"Lucene grammar element {node.__class__.__name__} "
            f"is not supported in search"
        )

    # Main function
    def build_query(self, query: str, queried_type: Optional[Type[Object]] = None):
        context = SQLQueryBuilderContext(queried_type=queried_type)
        tree = parser.parse(query)
        condition = self.visit(tree, context=context)
        return db.session.query(context.queried_type).filter(condition)
