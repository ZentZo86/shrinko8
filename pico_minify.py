from utils import *
from pico_tokenize import TokenType, tokenize, Token, k_char_escapes, CommentHint
from pico_tokenize import parse_string_literal, parse_fixnum, k_keep_prefix
from pico_tokenize import StopTraverse, k_skip_children
from pico_parse import Node, NodeType, VarKind
from pico_parse import k_unary_ops_prec, k_binary_op_precs, k_right_binary_ops

class Focus(Enum):
    values = ("none", "chars", "compressed")

# essentially only returns decvalue right now, given mostly non-fract. inputs
# TODO: test with fract-ish inputs to see what's best to do.
def format_fixnum(value, allow_minus=False):
    """format a fixnum to a pico8 string"""
    intvalue = value >> 16
    dotvalue = value & 0xffff

    hexvalue = "0x%x" % intvalue
    if dotvalue:
        hexvalue = "0x" if hexvalue == "0x0" else hexvalue
        hexvalue += (".%04x" % dotvalue).rstrip('0')
        
    def str_add_1(str):
        if not str:
            return "1"
        elif str[-1] == ".":
            return str_add_1(str[:-1]) + "."
        elif str[-1] == "9":
            return str_add_1(str[:-1]) + "0"
        else:
            return str[:-1] + chr(ord(str[-1]) + 1)
    
    numvalue = value / (1 << 16)
    decvalue = "%.10f" % numvalue
    while "." in decvalue:
        nextvalue = decvalue[:-1]
        nextupvalue = str_add_1(nextvalue)
        if parse_fixnum(nextvalue) == value:
            decvalue = nextvalue
        elif parse_fixnum(nextupvalue) == value:
            decvalue = nextupvalue
        else:
            break
    if decvalue.startswith("0."):
        decvalue = decvalue[1:]

    minvalue = hexvalue if len(hexvalue) < len(decvalue) else decvalue

    if allow_minus and value & 0x80000000 and value != 0x80000000:
        negvalue = "-" + format_fixnum(-value & 0xffffffff)
        if len(negvalue) < len(minvalue):
            minvalue = negvalue

    return minvalue

k_char_escapes_rev = {v: k for k, v in k_char_escapes.items() if k != '\n'}
k_char_escapes_rev.update({"\0": "0", "\x0e": "14", "\x0f": "15"})

k_char_escapes_rev_min = {k: v for k, v in k_char_escapes_rev.items() if k in "\0\n\r\"'\\"}

def format_string_literal(value, use_ctrl_chars=True, long=None, quote=None):
    """format a pico8 string to a pico8 string literal"""

    if long != False:
        if "\0" not in value and "\r" not in value and "]]" not in value:
            long_prefix = "\n" if value.startswith("\n") else ""
            # note: we never generate [=[]=] and the like, as pico doesn't like it much
            strlong = "[[%s%s]]" % (long_prefix, value)
            if long == True:
                return strlong
        else:
            strlong = None
            long = False

    if long != True:
        if quote is None:
            quote = '"' if value.count('"') <= value.count("'") else "'"

        exclude_esc = "'" if quote == '"' else '"'
            
        char_escapes_rev = k_char_escapes_rev_min if use_ctrl_chars else k_char_escapes_rev

        litparts = []
        for i, ch in enumerate(value):
            if ch in char_escapes_rev and ch != exclude_esc:
                esc = char_escapes_rev[ch]
                if esc.isdigit() and i + 1 < len(value) and value[i + 1].isdigit():
                    esc = esc.rjust(3, '0')
                litparts.append("\\" + esc)
            else:
                litparts.append(ch)

        strlit = '%s%s%s' % (quote, "".join(litparts), quote)
        if long == False:
            return strlit

    return strlong if len(strlong) < len(strlit) else strlit

def minify_string_literal(token, focus, value=None):
    if value is None:
        value = parse_string_literal(token.value)
    
    if focus == Focus.chars:
        return format_string_literal(value)
    else:
        # haven't found a good balanced heuristic for 'long' yet
        return format_string_literal(value, long=token.value.startswith('['))

def get_precedence(node):
    if node.type == NodeType.binary_op:
        return k_binary_op_precs[node.op]
    elif node.type == NodeType.unary_op:
        return k_unary_ops_prec

def is_right_assoc(node):
    if node.type == NodeType.binary_op:
        return node.op in k_right_binary_ops
    else:
        return False

def is_vararg_expr(node):
    return node.type in (NodeType.call, NodeType.varargs)

def minify_needs_comments(minify):
    # returns whether minify_code makes use of the tokens' comments
    return isinstance(minify, dict) and not minify.get("wspace", True)
    
def next_vline(vline):
    # in py3.9, could use math.nextafter...
    m, e = math.frexp(vline)
    return math.ldexp(m + sys.float_info.epsilon / 2, e)

def get_node_bodies(node):
    if node.type in (NodeType.if_, NodeType.elseif):
        yield node.then
        if node.else_:
            yield from get_node_bodies(node.else_)
    else:
        yield node.body

def analyze_code_for_minify(root, focus):
    shorts = CounterDictionary()
    longs = CounterDictionary()
    shortenables = set()

    def analyze_node_post(node):

        if node.type in (NodeType.if_, NodeType.while_):
            is_short = node.short

            weight = 1
            if node.type == NodeType.if_:
                else_ = node.else_
                while else_ and else_.type == NodeType.elseif:
                    weight += 1
                    else_ = else_.else_
            has_elseif = weight > 1

            # can the node be converted to shorthand?
            if not is_short and not has_elseif:
                has_shorthand, has_empties = False, False

                def check_shorthand(node):
                    nonlocal has_shorthand
                    # ideally, could allow last node in an 'if' to be a print...
                    if node.type == NodeType.print or (node.type in (NodeType.if_, NodeType.while_) and (node.short or node in shortenables)):
                        has_shorthand = True
                    
                # first check the parents
                node.traverse_parents(check_shorthand)
                
                # now check the children
                for body in get_node_bodies(node):
                    body.traverse_nodes(post=check_shorthand)
                    if not body.children:
                        has_empties = True
                
                # empty bodies require extra ';'s to shorten, which worsens compression
                is_short = not has_shorthand and not (has_empties and focus != Focus.chars)
                if is_short:
                    shortenables.add(node)
            
            if is_short:
                shorts[node.type] += weight
            else:
                longs[node.type] += weight

    root.traverse_nodes(post=analyze_node_post)

    new_shorts = {}
    for type in (NodeType.if_, NodeType.while_):
        if focus == Focus.chars or not longs[type] or (focus == Focus.none and longs[type] * 1.5 <= shorts[type]):
            new_shorts[type] = True
        elif focus == Focus.compressed:
            new_shorts[type] = False
        else:
            new_shorts[type] = None # leave alone

    return Dynamic(new_shorts=new_shorts, shortenables=shortenables)

def minify_change_shorthand(node, new_short):
    if new_short:
        node.short = True
        node.erase_token(2, "then" if node.type == NodeType.if_ else "do")
        if node.type == NodeType.if_ and node.else_:
            node.else_.short = True
            node.else_.erase_token(-1, "end")
        else:
            node.erase_token(-1, "end")
        
        # we can assume node.cond is not wrapped in parens, since we're in a post-visit
        # wrap it in parens ourselves (TODO: eww...)
        node.cond.replace_with(Node(NodeType.group, [], child=copy(node.cond)))
        node.cond.children.append(node.cond.child)
        node.cond.insert_token(0, TokenType.punct, "(", near_next=True)
        node.cond.append_token(TokenType.punct, ")")

        # fixup empty bodies
        for body in get_node_bodies(node):
            if not body.children:
                body.append_token(TokenType.punct, ";")

        vline = node.children[0].vline
        
        # ensure entire shorthand is on the same line
        def set_vline(token):
            token.vline = vline
        node.traverse_tokens(set_vline)

        # avoid further use of our vline...
        next = node.next_token()
        while next.vline == vline:
            next.vline = next_vline(vline)
            next = next.next_token()
        return vline

    else:
        node.short = False
        node.insert_token(2, TokenType.keyword, "then" if node.type == NodeType.if_ else "do")
        if node.type == NodeType.if_ and node.else_:
            node.else_.short = False
            node.else_.append_token(TokenType.keyword, "end", near_next=True)
        else:
            node.append_token(TokenType.keyword, "end", near_next=True)

def node_contains_vars(root, vars):
    def visitor(node):
        if node.type == NodeType.var and node.var in vars:
            raise StopTraverse()

    try:
        root.traverse_nodes(visitor)
        return False
    except StopTraverse:
        return True

def expr_is_trivial(root, ctxt, safe_only, allow_member, allow_index):
    def visitor(expr):
        # nodes that cannot call user-defined code in any case
        if expr.type in (NodeType.const, NodeType.varargs, NodeType.group,
                         NodeType.table, NodeType.table_member, NodeType.table_index): # (since new tables have no metatable)
            pass
        elif expr.type == NodeType.var and expr.kind != VarKind.global_:
            pass
        elif expr.type == NodeType.unary_op and expr.op == "not":
            pass
        elif expr.type == NodeType.binary_op and expr.op in ("and", "or"):
            pass
        elif expr.type == NodeType.function:
            assert not expr.target # we only traverse expressions!
            return k_skip_children
        # nodes that may call user-defined code
        elif expr.type == NodeType.call:
            func = expr.func
            if safe_only or not (func.type == NodeType.var and func.kind == VarKind.global_ and not func.var.reassigned and func.name not in ctxt.callback_builtins):
                raise StopTraverse()
        elif expr.type == NodeType.member and not allow_member:
            raise StopTraverse()
        elif expr.type == NodeType.index and not allow_index:
            raise StopTraverse()
        # nodes that may call user-defined code via metatables (E.g. member access, operators)
        elif safe_only:
            raise StopTraverse()
    
    try:
        root.traverse_nodes(visitor)
        return True
    except StopTraverse:
        return False

def minify_merge_assignments(prev, next, ctxt, safe_only):
    if len(prev.targets) < len(prev.sources):
        return
    if len(prev.targets) > len(prev.sources) and \
            ((prev.sources and is_vararg_expr(prev.sources[-1])) or (next.sources and is_vararg_expr(next.sources[-1])) or len(next.targets) < len(next.sources)):
        return
    
    # check if prev's targets are used in next's sources or targets

    require_trivial = False # True when prev.targets may be accessed indirectly from functions that may be called by next.soources
    allow_index = allow_member = True
    target_vars = []
    for target in prev.targets:
        if target.type == NodeType.var:
            target_vars.append(target.var)
            if target.kind == VarKind.global_ or (prev.type == NodeType.assign and target.var.captured):
                require_trivial = True
        elif target.type == NodeType.member:
            target_vars.append(target.key.var)
            require_trivial = True
            allow_index = False # TODO: could rely on rename's preserve logic
        elif target.type == NodeType.index:
            require_trivial = True
            allow_member = False # TODO: could rely on rename's preserve logic
            allow_index = False
        else: # just in case...
            return
    
    for node in next.sources + next.targets:
        if target_vars and node_contains_vars(node, target_vars):
            return
        if require_trivial and not expr_is_trivial(node, ctxt, safe_only, allow_member, allow_index):
            return
    
    # do the merge: (TODO: eww...)

    def move_array_items(dst_node, dst_arr, dst_arr_i, src_node, src_arr, src_arr_i, count):        
        dst_arr_i = default(dst_arr_i, len(dst_arr))
        count = default(count, len(src_arr) - src_arr_i)
        
        if not count:
            return

        if dst_arr_i < len(dst_arr):
            dst_i = dst_node.children.index(dst_arr[dst_arr_i])
            if dst_arr_i > 0:
                dst_i -= 1 # prev comma
        elif len(dst_arr):
            dst_i = dst_node.children.index(dst_arr[dst_arr_i - 1]) + 1
        elif dst_arr is dst_node.sources:
            dst_node.append_token(TokenType.punct, "=")
            dst_i = len(dst_node.children)
        else:
            # (this can happen as we move extra targets from 'prev' to 'next')
            assert dst_arr is dst_node.targets and not dst_node.sources
            dst_i = len(dst_node.children)

        src_i = src_node.children.index(src_arr[src_arr_i])
        if src_arr_i > 0:
            src_i -= 1 # prev comma

        for i in range(count):
            src_elem = src_arr[src_arr_i] # no + i, since deleted below
            dst_arr.insert(dst_arr_i + i, src_elem)

            if dst_arr_i + i > 0:
                dst_node.insert_token(dst_i, TokenType.punct, ",")
                dst_i += 1

            dst_node.insert_existing(dst_i, src_elem)
            dst_i += 1
            
            # we can directly modify src_node.children since we modify src_arr as well to match it
            del src_arr[src_arr_i]
            if src_arr_i + i > 0:
                del src_node.children[src_i]
            del src_node.children[src_i]

    if len(prev.targets) > len(prev.sources):
        # move the extra targets to set to nil - to the end (will get moved from next back to prev below)
        move_array_items(next, next.targets, None, prev, prev.targets, len(prev.sources), None)
    
    move_array_items(prev, prev.targets, None, next, next.targets, 0, None)
    move_array_items(prev, prev.sources, None, next, next.sources, 0, None)

    next.erase()

def minify_code(source, ctxt, root, minify):

    safe_only = False
    minify_lines = minify_wspace = minify_tokens = minify_comments = True
    focus = Focus.none
    if isinstance(minify, dict):
        safe_only = minify.get("safe-only", False)
        minify_lines = minify.get("lines", True)
        minify_wspace = minify.get("wspace", True)
        minify_tokens = minify.get("tokens", True)
        minify_comments = minify.get("comments", True)
        focus = Focus(minify.get("focus", "none"))

    analysis = analyze_code_for_minify(root, focus)

    shorthand_vlines = set()
        
    def fixup_nodes_pre(node):
        if minify_tokens:  

            # remove shorthands

            if node.type in (NodeType.if_, NodeType.while_) and node.short and (analysis.new_shorts[node.type] == False):
                minify_change_shorthand(node, False)
        
    def fixup_nodes_post(node):
        if minify_tokens:

            # create shorthands
            
            if node.type in (NodeType.if_, NodeType.while_) and not node.short and \
               (analysis.new_shorts[node.type] == True) and node in analysis.shortenables:
                vline = minify_change_shorthand(node, True)
                shorthand_vlines.add(vline)

            # merge assignments

            if False: # node.type in (NodeType.assign, NodeType.local):
                prev = node.prev_sibling()
                while prev and prev.type == None: # skip erased
                    prev = prev.prev_sibling()
                if prev and prev.type == node.type:
                    minify_merge_assignments(prev, node, ctxt, safe_only)

        # find shorthands

        if node.type == NodeType.print or (node.type in (NodeType.if_, NodeType.while_) and node.short):
            shorthand_vlines.add(node.children[0].vline)
                        
    def remove_parens(token):
        token.erase("(")
        token.parent.erase_token(-1, ")")

    def fixup_tokens(token):

        # minify sublangs

        sublang = getattr(token, "sublang", None)
        if sublang and sublang.minify:
            token.modify(minify_string_literal(token, focus, value=sublang.minify()))

        if minify_tokens:
            
            # remove unneeded tokens

            if token.value == ";" and token.parent.type == NodeType.block and token.next_token().value != "(":
                gparent = token.parent.parent
                if not (gparent and gparent.short and not token.parent.stmts):
                    token.erase()
                    return

            if token.value in (",", ";") and token.parent.type == NodeType.table and token.next_sibling().value == "}":
                token.erase()
                return

            if token.value == "(" and token.parent.type == NodeType.call and len(token.parent.args) == 1:
                arg = token.parent.args[0]
                if arg.type == NodeType.table or (arg.type == NodeType.const and arg.token.type == TokenType.string):
                    return remove_parens(token)

            if token.value == "(" and token.parent.type == NodeType.group:
                inner, outer = token.parent.child, token.parent.parent
                inner_prec, outer_prec = get_precedence(inner), get_precedence(outer)
                if e(inner_prec) and e(outer_prec) and (inner_prec > outer_prec or (inner_prec == outer_prec and
                        (outer_prec == k_unary_ops_prec or is_right_assoc(outer) == (outer.right == token.parent)))):
                    return remove_parens(token)
                if outer.type in (NodeType.group, NodeType.table_member, NodeType.table_index, NodeType.op_assign):
                    return remove_parens(token)
                if outer.type in (NodeType.call, NodeType.print) and (token.parent in outer.args[:-1] or 
                        (outer.args and token.parent == outer.args[-1] and not is_vararg_expr(inner))):
                    return remove_parens(token)
                if outer.type in (NodeType.assign, NodeType.local) and (token.parent in outer.sources[:-1] or 
                        (outer.sources and token.parent == outer.sources[-1] and (not is_vararg_expr(inner) or len(outer.targets) <= len(outer.sources)))):
                    return remove_parens(token)
                if outer.type in (NodeType.return_, NodeType.table) and (token.parent in outer.items[:-1] or
                        (outer.items and token.parent == outer.items[-1] and not is_vararg_expr(inner))):
                    return remove_parens(token)
                if outer.type in (NodeType.if_, NodeType.elseif, NodeType.while_, NodeType.until) and not outer.short:
                    return remove_parens(token)

            # replace tokens for higher consistency

            if token.value == ";" and token.parent.type == NodeType.table:
                token.modify(",")

            if token.value == "!=":
                token.modify("~=")
             
            if token.value == "^^" and ctxt.version >= 37:
                token.modify("~")

            if token.type == TokenType.string:
                token.modify(minify_string_literal(token, focus))

            if token.type == TokenType.number:
                outer_prec = get_precedence(token.parent.parent) if token.parent.type == NodeType.const else None
                allow_minus = outer_prec is None or outer_prec < k_unary_ops_prec
                token.modify(format_fixnum(parse_fixnum(token.value), allow_minus=allow_minus))
                if token.value.startswith("-"):
                    # insert synthetic minus token, so that output_tokens's tokenize won't get confused
                    token.modify(token.value[1:])
                    token.parent.insert_token(0, TokenType.punct, "-", near_next=True)

    root.traverse_nodes(fixup_nodes_pre, fixup_nodes_post, tokens=fixup_tokens)

    if minify_wspace:
        return output_min_wspace(root, shorthand_vlines, minify_lines)
    else:
        return output_original_wspace(root, minify_comments)

def need_whitespace_between(prev_token, token):
    combined = prev_token.value + token.value
    ct, ce = tokenize(PicoSource(None, combined))
    return ce or len(ct) != 2 or (ct[0].type, ct[0].value, ct[1].type, ct[1].value) != (prev_token.type, prev_token.value, token.type, token.value)

def output_min_wspace(root, shorthand_vlines, minify_lines=True):
    """convert a root back to a string, inserting as little whitespace as possible"""
    output = []
    prev_token = Token.none

    def output_tokens(token):
        nonlocal prev_token

        if token.children:
            for comment in token.children:
                if comment.hint == CommentHint.keep:
                    output.append(comment.value.replace(k_keep_prefix, "", 1))

        if token.value is None:
            return

        # (modified tokens may require whitespace not previously required - e.g. 0b/0x)
        if (prev_token.endidx < token.idx or prev_token.modified or token.modified) and prev_token.value:
            # TODO: always adding \n before if/while won a few bytes on my code - check if this is consistent & helpful.

            if prev_token.vline != token.vline and (not minify_lines or prev_token.vline in shorthand_vlines):
                output.append("\n")
            elif need_whitespace_between(prev_token, token):
                output.append(" ")

        output.append(token.value)
        prev_token = token

    root.traverse_tokens(output_tokens)
    return "".join(output)

def output_original_wspace(root, exclude_comments=False):
    """convert a root back to a string, using original whitespace (optionally except comments)"""
    output = []
    prev_token = Token.none
    prev_welded_token = None

    def output_tokens(token):
        nonlocal prev_token, prev_welded_token
        if prev_token.endidx != token.idx:
            wspace = token.source.text[prev_token.endidx:token.idx]
            if exclude_comments and token.children:
                # only output spacing before and after the comments between the tokens
                prespace = token.source.text[prev_token.endidx:token.children[0].idx]
                postspace = token.source.text[token.children[-1].endidx:token.idx]
                output.append(prespace)
                if "\n" in wspace and "\n" not in prespace and "\n" not in postspace:
                    output.append("\n")
                elif wspace and not prespace and not postspace:
                    output.append(" ")
                output.append(postspace)
            else:
                output.append(wspace)
            prev_welded_token = None
        
        # extra whitespace may be needed due to modified or deleted tokens
        if prev_welded_token and token.value and (prev_welded_token.modified or token.modified or prev_welded_token != prev_token):
            if need_whitespace_between(prev_welded_token, token):
                output.append(" ")

        if token.value != None:
            output.append(token.value)
            prev_welded_token = token
            
        prev_token = token
    
    root.traverse_tokens(output_tokens)
    return "".join(output)

from pico_process import PicoSource
