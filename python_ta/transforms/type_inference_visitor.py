import astroid.inference
import astroid
from astroid.node_classes import *
from typing import *
import typing
from typing import CallableMeta, TupleMeta, Union, _ForwardRef
from astroid.transforms import TransformVisitor
from ..typecheck.base import Environment, TypeConstraints, parse_annotations, \
    _node_to_type, TypeResult, TypeInfo, TypeFail, failable_collect, accept_failable, create_Callable_TypeResult, \
    wrap_container, NoType, TypeFailLookup, TypeFailFunction, TypeFailReturn, TypeFailStarred, _gorg,\
    TypeFailAnnotationInvalid
from ..typecheck.errors import BINOP_TO_METHOD, BINOP_TO_REV_METHOD, UNARY_TO_METHOD, \
    INPLACE_TO_BINOP, binop_error_message, unaryop_error_message
from ..typecheck.type_store import TypeStore


class TypeInferer:
    """The class responsible for inferring types given an astroid AST.
    """
    type_constraints = TypeConstraints()
    type_store = TypeStore(type_constraints)
    type_constraints.type_store = type_store

    def __init__(self) -> None:
        self.type_constraints.reset()

    def reset(self) -> None:
        self.type_constraints.reset()
        self.type_store = TypeStore(self.type_constraints)
        self.type_constraints.type_store = self.type_store

    ###########################################################################
    # Setting up the environment
    ###########################################################################
    def environment_transformer(self) -> TransformVisitor:
        """Return a TransformVisitor that sets an environment for every node."""
        visitor = TransformVisitor()
        visitor.register_transform(astroid.FunctionDef, self._set_function_def_environment)
        visitor.register_transform(astroid.AsyncFunctionDef, self._set_function_def_environment)
        visitor.register_transform(astroid.ClassDef, self._set_classdef_environment)
        visitor.register_transform(astroid.Module, self._set_module_environment)
        visitor.register_transform(astroid.ListComp, self._set_comprehension_environment)
        visitor.register_transform(astroid.DictComp, self._set_comprehension_environment)
        visitor.register_transform(astroid.SetComp, self._set_comprehension_environment)
        visitor.register_transform(astroid.GeneratorExp, self._set_comprehension_environment)
        visitor.register_transform(astroid.Lambda, self._set_comprehension_environment)
        return visitor

    def _set_module_environment(self, node: astroid.Module) -> None:
        """Method to set environment of a Module node."""
        node.type_environment = Environment()
        for name in node.globals:
            if not any(isinstance(elt, (astroid.ImportFrom, astroid.Import)) for elt in node.globals[name]):
                new_tvar = self.type_constraints.fresh_tvar(node.globals[name][0])
                if any(isinstance(elt, astroid.ClassDef) for elt in node.globals[name]):
                    self.type_constraints.unify(new_tvar, Type[_ForwardRef(name)], node)
                node.type_environment.globals[name] = new_tvar
        self._populate_local_env(node)

    def _set_classdef_environment(self, node: astroid.ClassDef) -> None:
        """Method to set environment of a ClassDef node."""
        node.type_environment = Environment()
        for name in node.instance_attrs:
            node.type_environment.locals[name] = self.type_constraints.fresh_tvar(node.instance_attrs[name][0])
            self.type_store.classes[node.name][name] = [(node.type_environment.locals[name], 'attribute')]
        for name in node.locals:
            node.type_environment.locals[name] = self.type_constraints.fresh_tvar(node.locals[name][0])

        self.type_store.classes[node.name]['__bases'] = [_node_to_type(base)
                                                         for base in node.bases]
        try:
            self.type_store.classes[node.name]['__mro'] = [cls.name for cls in node.mro()]
        except astroid.exceptions.DuplicateBasesError:
            self.type_store.classes[node.name]['__mro'] = [node.name]

    def _set_function_def_environment(self, node: astroid.FunctionDef) -> None:
        """Method to set environment of a FunctionDef node."""
        node.type_environment = Environment()
        # self is a special case
        if node.args.args and node.args.args[0].name == 'self' and isinstance(node.parent, astroid.ClassDef):
            node.type_environment.locals['self'] = _ForwardRef(node.parent.name)
        self._populate_local_env(node)
        node.type_environment.locals['return'] = self.type_constraints.fresh_tvar(node)

    def _set_comprehension_environment(self, node: astroid.Comprehension) -> None:
        """Set the environment of a comprehension expression.

        Covers ListComp, SetComp, DictComp, and GeneratorExp."""
        node.type_environment = Environment()
        for name in node.locals:
            node.type_environment.locals[name] = self.type_constraints.fresh_tvar(node)

    def _populate_local_env(self, node: NodeNG) -> None:
        """Helper to populate locals attributes in type environment of given node."""
        for var_name in node.locals:
            if not any(isinstance(elt, (astroid.ImportFrom, astroid.Import)) for elt in node.locals[var_name]):
                try:
                    var_value = node.type_environment.lookup_in_env(var_name)
                except KeyError:
                    var_value = self.type_constraints.fresh_tvar(node.locals[var_name][0])
                node.type_environment.locals[var_name] = var_value

    ###########################################################################
    # Type inference methods
    ###########################################################################
    def type_inference_transformer(self) -> TransformVisitor:
        """Instantiate a visitor to perform type inference on an AST.
        """
        type_visitor = TransformVisitor()
        for klass in astroid.ALL_NODE_CLASSES:
            if hasattr(self, f'visit_{klass.__name__.lower()}'):
                type_visitor.register_transform(klass, getattr(self, f'visit_{klass.__name__.lower()}'))
            else:
                type_visitor.register_transform(klass, self.visit_default)
        return type_visitor

    def visit_default(self, node: NodeNG) -> None:
        node.inf_type = NoType()

    ##############################################################################
    # Literals
    ##############################################################################
    def visit_const(self, node: astroid.Const) -> None:
        node.inf_type = TypeInfo(type(node.value))

    def visit_list(self, node: astroid.List) -> None:
        if node.ctx == astroid.Store:
            # List is the target of an assignment; do not give it a type.
            node.inf_type = NoType()
        elif not node.elts:
            node.inf_type = TypeInfo(List[self.type_constraints.fresh_tvar(node)])
        else:
            elt_inf_type = self._unify_elements(node.elts, node)
            node.inf_type = wrap_container(List, elt_inf_type)

    def visit_set(self, node: astroid.Set) -> None:
        if not node.elts:
            node.inf_type = TypeInfo(Set[self.type_constraints.fresh_tvar(node)])
        else:
            elt_inf_type = self._unify_elements(node.elts, node)
            node.inf_type = wrap_container(Set, elt_inf_type)

    def visit_dict(self, node: astroid.Dict) -> None:
        if not node.items:
            node.inf_type = TypeInfo(Dict[self.type_constraints.fresh_tvar(node),
                                          self.type_constraints.fresh_tvar(node)])
        else:
            key_list, val_list = zip(*node.items)
            key_inf_type = self._unify_elements(key_list, node)
            val_inf_type = self._unify_elements(val_list, node)
            node.inf_type = wrap_container(Dict, key_inf_type, val_inf_type)

    def visit_tuple(self, node: astroid.Tuple) -> None:
        if node.ctx == astroid.Store:
            # Tuple is the target of an assignment; do not give it a type.
            node.inf_type = NoType()
        else:
            node.inf_type = wrap_container(Tuple, *(e.inf_type for e in node.elts))

    def _unify_elements(self, lst: List[NodeNG], node: NodeNG) -> TypeResult:
        lst = list(lst)
        elt_inf_type = lst[0].inf_type
        for cur_elt in lst:
            elt_inf_type = self.type_constraints.unify(elt_inf_type, cur_elt.inf_type, node)
            if isinstance(elt_inf_type, TypeFail):
                return TypeInfo(Any)

        return elt_inf_type

    ##############################################################################
    # Expression types
    ##############################################################################
    def visit_ifexp(self, node: astroid.IfExp) -> None:
        node.inf_type = self.type_constraints.unify(node.body.inf_type, node.orelse.inf_type, node)

    def visit_expr(self, node: astroid.Expr) -> None:
        """Expr nodes take the type of their child.
        """
        node.inf_type = node.value.inf_type

    ##############################################################################
    # Name lookup and assignment
    ##############################################################################
    def visit_name(self, node: astroid.Name) -> None:
        node.inf_type = self.lookup_inf_type(node, node.name)

    def visit_assign(self, node: astroid.Assign) -> None:
        """Update the enclosing scope's type environment for the assignment's binding(s)."""
        # the type of the expression being assigned
        if isinstance(node.value, astroid.Name):
            expr_inf_type = self.lookup_typevar(node, node.value.name)
        else:
            expr_inf_type = node.value.inf_type

        node.inf_type = NoType()

        for target in node.targets:
            type_result = self._assign_type(target, expr_inf_type, node)
            if isinstance(type_result, TypeFail):
                node.inf_type = type_result
                break

    def visit_annassign(self, node: astroid.AnnAssign) -> None:
        if isinstance(node.target, astroid.AssignAttr):
            var_inf_type = self.lookup_typevar(node.target, node.target.attrname)
        else:
            var_inf_type = self.lookup_typevar(node.target, node.target.name)
        ann_type = self._ann_node_to_type(node.annotation)
        self.type_constraints.unify(var_inf_type, ann_type, node)
        if node.value:
            node.targets = [node.target]
            self.visit_assign(node)
        elif isinstance(ann_type, TypeFail):
            node.inf_type = ann_type
        else:
            node.inf_type = NoType()

    def _ann_node_to_type(self, node: astroid.Name) -> TypeResult:
        """Return a type represented by the input node, substituting Any for missing arguments in generic types
        """
        try:
            ann_node_type = _node_to_type(node)
        except SyntaxError:
            # Attempted to create ForwardRef with invalid string
            return TypeFailAnnotationInvalid(node)

        if isinstance(ann_node_type, GenericMeta) and ann_node_type.__args__ is None:
            if ann_node_type == Dict:
                ann_type = wrap_container(ann_node_type, Any, Any)
            elif ann_node_type == Tuple:
                # TODO: Add proper support for multi-parameter Tuples
                ann_type = wrap_container(ann_node_type, Any)
            else:
                ann_type = wrap_container(ann_node_type, Any)
        elif not isinstance(ann_node_type, type) and not isinstance(ann_node_type, _ForwardRef):
            ann_type = TypeFailAnnotationInvalid(node)
        else:
            ann_type = TypeInfo(ann_node_type)
        return ann_type

    def visit_augassign(self, node: astroid.AugAssign) -> None:
        node.inf_type = NoType()

        # lookup method for augmented arithmetic assignment
        method_name = BINOP_TO_METHOD[node.op]
        if isinstance(node.target, astroid.Subscript):
            target_type = node.target.value.inf_type
            binop_result = self._handle_call(node.target, '__setitem__', target_type,
                                             node.target.slice.inf_type, node.value.inf_type)
        else:
            if isinstance(node.target, astroid.AssignName):
                target_type = self.lookup_typevar(node.target, node.target.name)
            elif isinstance(node.target, astroid.AssignAttr):
                target_type = self._lookup_attribute_type(node.target, node.target.expr.inf_type,
                                                          node.target.attrname)
            binop_result = self._handle_call(node, method_name, target_type, node.value.inf_type)
        if isinstance(binop_result, TypeFail):
            # on failure, fallback to method corresponding to standard operator
            boolop = INPLACE_TO_BINOP[node.op]
            method_name = BINOP_TO_METHOD[boolop]
            arithm_type = self._arithm_convert(node, method_name, target_type, node.value.inf_type)
            if arithm_type:
                binop_result = arithm_type
            else:
                binop_result = self._handle_call(node, method_name, target_type, node.value.inf_type)

        type_result = self._assign_type(node.target, binop_result, node)
        if isinstance(type_result, TypeFail):
            node.inf_type = type_result

    @accept_failable
    def _assign_type(self, target: NodeNG, expr_type: type, node: astroid.Assign) -> TypeResult:
        """Update the type environment so that the target is bound to the given type."""
        if isinstance(target, astroid.AssignName):
            # A single identifier, e.g. x = ...
            target_type_var = self.lookup_typevar(target, target.name)
            return self.type_constraints.unify(target_type_var, expr_type, node)
        elif isinstance(target, astroid.AssignAttr):
            # Attribute mutation, e.g. x.y = ...
            attr_type = self._lookup_attribute_type(target, target.expr.inf_type, target.attrname)
            return self.type_constraints.unify(attr_type, expr_type, node)
        elif isinstance(target, astroid.Tuple):
            # Unpacking assignment, e.g. x, y = ...
            if isinstance(expr_type, typing.TupleMeta):
                assign_result = self._assign_tuple(target, expr_type, node)
            else:
                assign_result = self._handle_call(target, '__iter__', expr_type)
                for subtarget in target.elts:
                    if isinstance(subtarget, astroid.Starred):
                        target_tvar = self.lookup_typevar(subtarget.value, subtarget.value.name)
                        unif_result = assign_result >> (
                            lambda t: self.type_constraints.unify(target_tvar, List[t.__args__[0]], node))
                    else:
                        target_tvar = self.lookup_typevar(subtarget, subtarget.name)
                        unif_result = assign_result >> (
                            lambda t: self.type_constraints.unify(target_tvar, t.__args__[0], node))

                    if isinstance(unif_result, TypeFail):
                        return unif_result
            return assign_result
        elif isinstance(target, astroid.Subscript):
            # TODO: previous case must recursively handle this one
            return self._handle_call(target, '__setitem__', target.value.inf_type, target.slice.inf_type, expr_type)

    def _assign_tuple(self, target: astroid.Tuple, value: TupleMeta, node: astroid.Assign) -> TypeResult:
        """Unify tuple of type variables and tuple of types, within context of Assign statement."""
        starred_index = None
        for i in range(len(target.elts)):
            if isinstance(target.elts[i], astroid.Starred):
                if starred_index is None:
                    starred_index = i
                else:
                    return TypeFailStarred(node)

        target_tvars = []
        for subtarget in target.elts:
            if isinstance(subtarget, astroid.AssignAttr):
                target_tvars.append(self._lookup_attribute_type(subtarget, subtarget.expr.inf_type, subtarget.attrname))
            elif isinstance(subtarget, astroid.Starred):
                if isinstance(subtarget.value, astroid.AssignAttr):
                    target_tvars.append(self.lookup_typevar(subtarget.value, subtarget.value.attrname))
                else:
                    target_tvars.append(self.lookup_typevar(subtarget.value, subtarget.value.name))
            elif isinstance(subtarget, astroid.Subscript):
                target_tvars.append(self._handle_call(subtarget, '__getitem__', subtarget.value.inf_type,
                                                      subtarget.slice.inf_type))
            else:
                target_tvars.append(self.lookup_typevar(subtarget, subtarget.name))

        if starred_index is not None:
            starred_length = len(value.__args__) - len(target.elts) + 1
            starred_subvalues = node.value.elts[starred_index:starred_index+starred_length]
            starred_value = wrap_container(List, self._unify_elements(starred_subvalues, node))

            starred_target_tvar = target_tvars[starred_index]

            unif_result = self.type_constraints.unify(starred_target_tvar, starred_value, node)
            if isinstance(unif_result, TypeFail):
                return unif_result

            nonstarred_values = Tuple[value.__args__[:starred_index] + value.__args__[starred_index + starred_length:]]
            nonstarred_targets = target_tvars
            nonstarred_targets.remove(nonstarred_targets[starred_index])

        else:
            nonstarred_values = value
            nonstarred_targets = target_tvars

        nonstarred_target_tuple = wrap_container(
            Tuple, *nonstarred_targets)

        unif_result = self.type_constraints.unify(nonstarred_target_tuple, nonstarred_values, node)
        if isinstance(unif_result, TypeFail):
            return unif_result

        assign_result = TypeInfo(value)
        return assign_result

    @accept_failable
    def _lookup_attribute_type(self, node: NodeNG, class_type: type, attribute_name: str) -> TypeResult:
        """Given the node, class and attribute name, return the type of the attribute."""
        class_name, _, _ = self.get_attribute_class(class_type)
        closest_frame = node.scope().lookup(class_name)[0]
        class_env = closest_frame.locals[class_name][0].type_environment
        return self.type_constraints.resolve(class_env.lookup_in_env(attribute_name))

    def lookup_typevar(self, node: NodeNG, name: str) -> TypeResult:
        """Given a variable name, return the equivalent TypeVar in the closest scope relative to given node."""
        cur_node = node

        while cur_node is not None:
            # Get first parent node with scope
            cur_scope = cur_node.scope()
            try:
                # Attempt to look up variable in type environment
                return TypeInfo(cur_scope.type_environment.lookup_in_env(name))
            except KeyError:
                # Variable not found in scope of current node, search parent node
                cur_node = cur_scope.parent

        # If root of astroid tree is reached with no variable found,
        # search builtins and TypeStore for variable type
        if name in self.type_store.classes:
            result = TypeInfo(Type[__builtins__[name]])
        elif name.lower() in self.type_store.classes:
            result = TypeInfo(Type[__builtins__[name.lower()]])
        elif name in self.type_store.functions:
            result = TypeInfo(self.type_store.functions[name][0][0])
        else:
            result = TypeFail("Unbound identifier")

        return result

    def lookup_inf_type(self, node: NodeNG, name: str) -> TypeResult:
        """Given a variable name, return a TypeResult object containing the type in the closest scope relative to given node.
        """
        tvar = self.lookup_typevar(node, name)
        return self.type_constraints.resolve(tvar)

    ##############################################################################
    # Operation nodes
    ##############################################################################
    @accept_failable
    def get_call_signature(self, c: type, node: NodeNG) -> TypeResult:
        """Check for and return initializer function signature when using class name as Callable.
        Return Callable unmodified otherwise.

        :param c: Class, _ForwardRef to a class, or Callable
        :param node: astroid.Call node where function call is occurring
        """
        # Callable type; e.g., 'Callable[[int], int]'
        if isinstance(c, CallableMeta):
            return TypeInfo(c)
        # Union of Callables
        elif c.__class__.__name__ == '_Union' and all(isinstance(elt, CallableMeta) for elt in c.__args__):
            return TypeInfo(c)
        # Class types; e.g., 'Type[_ForwardRef('A')]'
        elif getattr(c, '__name__', None) == 'Type':
            class_type = c.__args__[0]
            if isinstance(class_type, _ForwardRef):
                class_name = c.__args__[0].__forward_arg__
            else:
                class_name = class_type.__name__

            if '__init__' in self.type_store.classes[class_name]:
                init_args = list(self.type_store.classes[class_name]['__init__'][0][0].__args__)
                init_func = Callable[init_args[1:-1], init_args[0]]
            else:
                # Classes declared without initializer
                init_func = Callable[[], class_type]
            return TypeInfo(init_func)
        # Class instances; e.g., '_ForwardRef('A')'
        elif isinstance(c, _ForwardRef):
            class_type = c
            class_name = c.__forward_arg__

            if '__call__' in self.type_store.classes[class_name]:
                call_args = list(self.type_store.classes[class_name]['__call__'][0][0].__args__)
                call_func = Callable[call_args[1:-1], call_args[-1]]
                return TypeInfo(call_func)
            else:
                class_tnode = self.type_constraints.get_tnode(class_type)
                return TypeFailLookup(class_tnode, node, node.parent)
        else:
            return TypeFailFunction((c,), None, node)

    def visit_call(self, node: astroid.Call) -> None:
        func_inf_type = self.get_call_signature(node.func.inf_type, node.func)
        arg_inf_types = [arg.inf_type for arg in node.args]
        node.inf_type = self.type_constraints.unify_call(func_inf_type, *arg_inf_types, node=node)

    def visit_binop(self, node: astroid.BinOp) -> None:
        left_inf, right_inf = node.left.inf_type, node.right.inf_type

        method_name = BINOP_TO_METHOD[node.op]
        # attempt to obtain a common arithmetic type
        arithm_type = self._arithm_convert(node, method_name, left_inf, right_inf)
        if arithm_type:
            node.inf_type = arithm_type
        else:
            rev_method_name = BINOP_TO_REV_METHOD[node.op]
            l_type = self._handle_call(node, method_name, left_inf, right_inf,
                                                  error_func=binop_error_message)
            r_type = self._handle_call(node, rev_method_name, right_inf, left_inf,
                                                  error_func=binop_error_message)

            if self.type_store.is_descendant(right_inf.getValue(), left_inf.getValue()):
                if isinstance(r_type, TypeFail) and isinstance(l_type, TypeInfo):
                    node.inf_type = l_type
                else:
                    node.inf_type = r_type
            else:
                if isinstance(l_type, TypeFail) and isinstance(r_type, TypeInfo):
                    node.inf_type = r_type
                else:
                    node.inf_type = l_type

    @accept_failable
    def _arithm_convert(self, node: NodeNG, method: str, t1_: type, t2_: type) -> Optional[TypeInfo]:
        common_type = None
        for t1, t2 in [(t1_, t2_), (t2_, t1_)]:
            if t1 is complex and self.type_store.is_descendant(t2, SupportsComplex):
                common_type = complex
            if t1 is float and self.type_store.is_descendant(t2, SupportsFloat):
                common_type = float
        if common_type:
            return self._handle_call(node, method, common_type, common_type)
        else:
            return None

    def visit_unaryop(self, node: astroid.UnaryOp) -> None:
        # 'not' is not a function, so this handled as a separate case.
        if node.op == 'not':
            node.inf_type = TypeInfo(bool)
        else:
            method_name = UNARY_TO_METHOD[node.op]
            node.inf_type = self._handle_call(node, method_name, node.operand.inf_type, error_func=unaryop_error_message)

    def visit_boolop(self, node: astroid.BoolOp) -> None:
        node.inf_type = self._unify_elements(node.values, node)
        if isinstance(node.inf_type, TypeFail):
            node.inf_type = TypeInfo(Any)

    def _handle_compare(self, node: NodeNG, comparator: str, left: NodeNG, right: NodeNG) -> TypeResult:
        """Helper function to lookup a comparator, find the equivalent function call,
        and unify call with given arguments.
        """
        if comparator == 'is' or comparator == 'is not':
            return TypeInfo(bool)
        elif comparator == 'in' or comparator == 'not in':
            return self._handle_call(node, BINOP_TO_METHOD[comparator],
                                     right.inf_type, left.inf_type)
        else:
            return self._handle_call(node, BINOP_TO_METHOD[comparator],
                                     left.inf_type, right.inf_type)

    def visit_compare(self, node: astroid.Compare) -> None:
        left = node.left
        compare_type = self._handle_compare(node, node.ops[0][0], left, node.ops[0][1])

        for comparator, right in node.ops[1:]:
            resolved_type = self._handle_compare(node, comparator, left, right)
            compare_type = self.type_constraints.unify(compare_type, resolved_type, node)

        node.inf_type = compare_type

    ##############################################################################
    # Subscripting
    ##############################################################################
    def visit_index(self, node: astroid.Index) -> None:
        node.inf_type = node.value.inf_type

    def visit_slice(self, node: astroid.Slice) -> None:
        lower_type = node.lower.inf_type if node.lower else type(None)
        upper_type = node.upper.inf_type if node.upper else type(None)
        step_type = node.step.inf_type if node.step else type(None)
        node.inf_type = self._handle_call(node, '__init__', slice, lower_type,
                                          upper_type, step_type)
        node.inf_type = node.inf_type >> (
            lambda t: TypeInfo(slice) if t == type(None) else TypeInfo(t))

    def visit_extslice(self, node: astroid.ExtSlice):
        unif_res = failable_collect(dim.inf_type for dim in node.dims)
        node.inf_type = unif_res >> (
            lambda lst: wrap_container(Tuple, *lst))

    def visit_subscript(self, node: astroid.Subscript) -> None:
        if isinstance(node.slice.inf_type, TypeFail):
            node.inf_type = node.slice.inf_type
        elif node.ctx == astroid.Load:
            try:
                value_gorg = node.value.inf_type >> _gorg
            except AttributeError:
                value_gorg = None

            if value_gorg == Type:
                if isinstance(node.slice.value, astroid.Tuple):
                    node.inf_type = wrap_container(_node_to_type(node.value), *_node_to_type(node.slice.value))
                else:
                    node.inf_type = wrap_container(_node_to_type(node.value), _node_to_type(node.slice.value))
            else:
                node.inf_type = self._handle_call(node, '__getitem__', node.value.inf_type, node.slice.inf_type)
        elif node.ctx == astroid.Store:
            node.inf_type = NoType()
        elif node.ctx == astroid.Del:
            node.inf_type = self._handle_call(node, '__delitem__', node.value.inf_type, node.slice.inf_type)

    ##############################################################################
    # Loops
    ##############################################################################
    def visit_for(self, node: Union[astroid.For, astroid.Comprehension]) -> None:
        iter_type_result = self._handle_call(node, '__iter__', node.iter.inf_type)
        if isinstance(node.target, astroid.AssignName):
            target_inf_type = self.lookup_inf_type(node.target, node.target.name)
        else:
            target_inf_type = wrap_container(
                Tuple, (self.lookup_inf_type(subtarget, subtarget.name) for subtarget in node.target.elts))
        iter_type_result >> (
            lambda t: self.type_constraints.unify(t.__args__[0], target_inf_type, node))
        node.inf_type = iter_type_result if isinstance(iter_type_result, TypeFail) else NoType()

    ##############################################################################
    # Comprehensions
    ##############################################################################
    def visit_comprehension(self, node: astroid.Comprehension) -> None:
        self.visit_for(node)

    def visit_dictcomp(self, node: astroid.DictComp) -> None:
        key_inf_type = self.type_constraints.resolve(node.key.inf_type)
        val_inf_type = self.type_constraints.resolve(node.value.inf_type)
        node.inf_type = wrap_container(Dict, key_inf_type, val_inf_type)

    def visit_generatorexp(self, node: astroid.GeneratorExp) -> None:
        elt_inf_type = self.type_constraints.resolve(node.elt.inf_type)
        node.inf_type = wrap_container(Generator, elt_inf_type, None, None)

    def visit_listcomp(self, node: astroid.ListComp) -> None:
        val_inf_type = self.type_constraints.resolve(node.elt.inf_type)
        node.inf_type = wrap_container(List, val_inf_type)

    def visit_setcomp(self, node: astroid.SetComp) -> None:
        elt_inf_type = self.type_constraints.resolve(node.elt.inf_type)
        node.inf_type = wrap_container(Set, elt_inf_type)

    @accept_failable
    def _handle_call(self, node: NodeNG, function_name: str, *arg_types: type,
                     error_func: Optional[Callable[[NodeNG], str]] = None) -> TypeResult:
        """Helper to lookup a function and unify it with given arguments.
           Return the return type of unified function call.
        """
        arg_inf_types = [self.type_constraints.resolve(arg) for arg in arg_types]
        func_type = self.type_store.lookup_method(function_name, *arg_inf_types, node=node)

        if isinstance(func_type, TypeFail) and error_func is not None:
            return TypeFail(error_func(node))
        else:
            return self.type_constraints.unify_call(func_type, *arg_types, node=node)

    ##############################################################################
    # Definitions
    ##############################################################################
    def visit_functiondef(self, node: astroid.FunctionDef) -> None:
        node.inf_type = NoType()

        # Get the inferred type of the function arguments
        inferred_args = [self.lookup_inf_type(node, arg) for arg in node.argnames()]

        if isinstance(node.parent, astroid.ClassDef) and inferred_args:
            # first argument is special in these cases
            if node.type == 'method':
                self.type_constraints.unify(inferred_args[0], _ForwardRef(node.parent.name), node)
            elif node.type == 'classmethod':
                self.type_constraints.unify(inferred_args[0], Type[_ForwardRef(node.parent.name)], node)

        # Get inferred return type
        if any(node.nodes_of_class(astroid.Return)):
            return_node = list(node.nodes_of_class(astroid.Return))[-1]
            if isinstance(return_node.inf_type, TypeFail):
                inferred_return = return_node.inf_type
            else:
                inferred_return = self.lookup_inf_type(node, 'return')
        elif node.name == '__init__':
            inferred_return = inferred_args[0]
        else:
            inferred_return = TypeInfo(type(None))

        # Update the environment storing the function's type.
        polymorphic_tvars = []
        for arg in inferred_args + [inferred_return]:
            arg >> (
                lambda a: polymorphic_tvars.append(a.__name__) if isinstance(a, TypeVar) else None)

        # Create function signature
        func_type = create_Callable_TypeResult(failable_collect(inferred_args), inferred_return, polymorphic_tvars)

        # Check for optional arguments, create a Union of function signatures if necessary
        num_defaults = len(node.args.defaults)
        if num_defaults > 0 and not isinstance(func_type, TypeFail):
            for i in range(num_defaults):
                opt_args = inferred_args[:-1-i]
                opt_func_type = create_Callable_TypeResult(failable_collect(opt_args), inferred_return, polymorphic_tvars)
                func_type = func_type >> (
                    lambda f: opt_func_type >> (
                        lambda opt_f: TypeInfo(Union[f, opt_f])))

        # Final type signature unify
        func_name = self.lookup_inf_type(node.parent, node.name)
        result = self.type_constraints.unify(func_name, func_type, node)
        if isinstance(result, TypeFail):
            node.inf_type = result

    def visit_asyncfunctiondef(self, node: astroid.AsyncFunctionDef) -> None:
        self.visit_functiondef(node)

    def visit_arguments(self, node: astroid.Arguments) -> None:
        node.inf_type = NoType()
        if any(annotation is not None for annotation in node.annotations):
            for i in range(len(node.annotations)):
                arg_tvar = self.lookup_typevar(node, node.args[i].name)

                if node.annotations[i] is not None:
                    ann_type = self._ann_node_to_type(node.annotations[i])
                    result = self.type_constraints.unify(
                        arg_tvar, ann_type, node)
                    if isinstance(result, TypeFail):
                        node.inf_type = result
                else:
                    self.type_constraints.unify(
                        arg_tvar, Any, node)

    def visit_return(self, node: astroid.Return) -> None:
        return_tvar = self.lookup_typevar(node, 'return')
        # TODO: Replace with isinstance() once proper TypeFail subclass is created for unbound indentifiers
        if return_tvar == TypeFail("Unbound identifier"):
            return_target = TypeFailReturn(node)
        else:
            return_target = return_tvar

        if node.value is not None and node.scope().returns is not None:
            return_annotation = _node_to_type(node.scope().returns)
            return_value = self.type_constraints.unify(node.value.inf_type, return_annotation, node)
        elif node.value is not None:
            return_value = node.value.inf_type
        else:
            return_value = TypeInfo(None)

        val_inf_type = self.type_constraints.unify(return_value, return_target, node)
        node.inf_type = val_inf_type if isinstance(val_inf_type, TypeFail) else NoType()

    def visit_classdef(self, node: astroid.ClassDef) -> None:
        node.inf_type = NoType()

        # Update type_store for this class.
        # TODO: include node.instance_attrs as well?
        for attr in node.locals:
            attr_inf_type = self.type_constraints.resolve(node.type_environment.lookup_in_env(attr))
            attr_inf_type >> (
                lambda a: self.type_store.methods[attr].append((a, node.locals[attr][0].type)) if isinstance(a, CallableMeta) else None)
            attr_inf_type >> (
                lambda a: self.type_store.classes[node.name][attr].append((a, node.locals[attr][0].type if isinstance(a, CallableMeta) else 'attribute')))

    @accept_failable
    def get_attribute_class(self, t: type) -> Tuple[str, type, bool]:
        """Check for and return name and type of class represented by type t."""
        is_inst_expr = True

        # Class type: e.g., 'Type[_ForwardRef('A')]'
        if getattr(t, '__name__', None) == 'Type':
            class_type = t.__args__[0]
            is_inst_expr = False
        # Instance of class or builtin type; e.g., '_ForwardRef('A')' or 'int'
        else:
            class_type = t

        if isinstance(class_type, _ForwardRef):
            class_name = class_type.__forward_arg__
        else:
            class_name = getattr(t, '__name__', None)

        # TODO: the condition below is too general
        if class_name is not None and class_name not in self.type_store.classes:
            class_name = class_name.lower()

        return class_name, class_type, is_inst_expr

    def visit_attribute(self, node: astroid.Attribute) -> None:
        expr_inf_type = node.expr.inf_type
        result = self.get_attribute_class(expr_inf_type)

        if not isinstance(result, TypeFail):
            class_name, class_type, inst_expr = result

            if class_name in self.type_store.classes:
                attribute_type = None
                for par_class_type in self.type_store.classes[class_name]['__mro']:
                    attribute_type = self.type_store.classes[par_class_type].get(node.attrname)
                    if attribute_type:
                        break
                if attribute_type is None:
                    class_tnode = self.type_constraints.get_tnode(class_type)
                    node.inf_type = TypeFailLookup(class_tnode, node, node.parent)
                else:
                    func_type, method_type = attribute_type[0]
                    if isinstance(func_type, CallableMeta) and \
                            method_type == 'method' and inst_expr or \
                            method_type == 'classmethod':
                        func_type = self.type_constraints.fresh_callable(func_type, node)
                        self.type_constraints.unify(func_type.__args__[0], class_type)
                        func_type.__args__ = func_type.__args__[1:]
                    node.inf_type = TypeInfo(func_type)
            else:
                class_tnode = self.type_constraints.get_tnode(class_type)
                node.inf_type = TypeFailLookup(class_tnode, node, node.parent)
        else:
            node.inf_type = result

    def visit_module(self, node: astroid.Module) -> None:
        node.inf_type = NoType()


# Main function (useful for quick debugging)
def main(source: str) -> Tuple[astroid.Module, TypeInferer]:
    """Parse a string representing source text, and perform a typecheck.

    Return the astroid Module node (with the type_constraints attribute set
    on all nodes in the tree) and TypeInferer object.
    """
    module = astroid.parse(source)
    type_inferer = TypeInferer()
    type_inferer.environment_transformer().visit(module)
    type_inferer.type_inference_transformer().visit(module)
    return module, type_inferer
