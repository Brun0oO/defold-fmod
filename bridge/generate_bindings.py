import sys
import re
from pycparser import parse_file, c_ast
from jinja2 import Environment, FileSystemLoader, select_autoescape

TypeBasic = 1
TypeStruct = 2
TypeClass = 3
TypePointer = 4
TypeUnknown = 5

exclusions = {
    "FMOD_VECTOR": True,
}

ref_counted = {
    "FMOD_STUDIO_EVENTINSTANCE": True,
}

accessors = {
    "input_deref": "*",
    "input_ptr": "&",
    "output": "&",
}

arg_usage_overrides = {
    "FMOD_System_CreateSound": {
        "exinfo": "input"
    },
    "FMOD_System_CreateStream": {
        "exinfo": "input"
    },
}

optional_arguments = {
    "FMOD_System_CreateSound": {
        "exinfo": True
    },
    "FMOD_System_CreateStream": {
        "exinfo": True
    },
    "FMOD_System_PlaySound": {
        "channelgroup": True,
        "paused": True,
    },
}

valid = re.compile(r"^_*(IDs|[A-Z][a-z]+|[A-Z0-9]+(?![a-z]))")
def to_snake_case(s):
    components = []
    while True:
        match = valid.match(s)
        if match == None:
            break
        components.append(match.group(1).lower())
        s = s[match.end():]
    return "_".join(components)

enum_re = re.compile(r"^\s*#define FMOD_([a-zA-Z0-9_]+)")
enum_exceptions = {"STUDIO_COMMON_H": True, "STUDIO_H": True}
def add_defined_enums(enums):
    headers = ["include/fmod_common.h", "include/fmod_studio_common.h"]
    for filename in headers:
        with open(filename, "r") as f:
            line = f.readline()
            while line:
                match = enum_re.match(line)
                if match != None:
                    enum = match.group(1)
                    if not enum.startswith("PRESET_") and not enum in enum_exceptions:
                        enums.append(enum)
                line = f.readline()

def generate_bindings(ast):
    types = {}
    enums = []
    functions = []
    global_functions = []
    structs = {}
    basic_types = {}
    enum_types = []

    class ParsedTypeDecl:
        def __init__(self, *, node=None, name=None, c_type="", type=TypeBasic, readable=True, writeable=True):
            if node != None:
                if isinstance(node, c_ast.PtrDecl):
                    child = ParsedTypeDecl(node=node.type)
                    self.name = "ptr_" + child.name
                    self.c_type = child.c_type + "*"
                    self.const = "const" in node.quals
                    self.type = TypePointer

                    if self.const:
                        self.c_type = self.c_type + " const"

                    base_type = types[child.name] if child.name in types else TypeUnknown
                    self.readable = base_type == TypeStruct or base_type == TypeClass or child.c_type == "char"
                    self.writeable = False

                    self.child = child
                    return

                if isinstance(node, c_ast.TypeDecl) and isinstance(node.type, c_ast.IdentifierType):
                    name = "_".join(node.type.names)
                    c_type = " ".join(node.type.names)
                    const = ("const" in node.quals)

                    if const:
                        c_type = "const " + c_type

                    self.name = name
                    self.c_type = c_type
                    self.const = const

                    if name in basic_types:
                        other = basic_types[name]
                        self.name = other.name
                        self.type = other.type
                        self.readable = other.readable
                        self.writeable = other.writeable
                        return

                    self.type = types[name] if name in types else TypeUnknown
                    self.readable = self.type == TypeStruct
                    self.writeable = self.type == TypeStruct
                    return

                self.name = '__UNKNOWN__'
                self.c_type = '__UNKNOWN__'
                self.const = False
                self.type = TypeUnknown
                self.readable = False
                self.writable = False

            else:
                self.c_type = c_type
                self.name = name if name != None else re.sub(" ", "_", c_type)
                self.const = False
                self.type = type
                self.readable = readable
                self.writeable = writeable

    class ParsedStruct:
        def __init__(self):
            self.methods = []
            self.properties = []
            self.ref_counted = False

        def parse_struct(self, node):
            self.name = node.name
            self.is_class = False

            constructor_name = node.name
            constructor_name = re.sub("^FMOD_STUDIO_", "", constructor_name)
            self.constructor_table = -1
            if constructor_name == node.name:
                self.constructor_table = -2
                constructor_name = re.sub("^FMOD_", "", constructor_name)
            constructor_name = re.sub("^([0-9])", r"_\1", constructor_name)
            self.constructor_name = constructor_name

            properties = self.properties
            class StructVisitor(c_ast.NodeVisitor):
                def visit_Decl(self, node):
                    if node.name != None:
                        type_decl = ParsedTypeDecl(node=node.type)
                        properties.append((node.name, type_decl))

            StructVisitor().visit(node)

        def make_class(self, name):
            self.name = name
            self.is_class = True
            self.ref_counted = node.name in ref_counted

    class MethodArgument:
        def __init__(self, node):
            self.name = node.name
            self.arg_index = 0
            self.optional = False
            type = ParsedTypeDecl(node=node.type)
            self.type = type
            self.usage = "unknown"
            if type.name in basic_types and not (type.type == TypePointer and not type.child.const):
                self.usage = "input"
            elif type.type == TypeStruct:
                self.usage = "input_deref"
            elif type.type == TypePointer:
                if type.child.type == TypeClass:
                    self.usage = "input"
                elif (type.child.type == TypeStruct and type.child.const):
                    self.usage = "input"
                elif (type.child.type == TypeBasic and type.child.const):
                    self.usage = "input_ptr"
                elif not type.child.const:
                    child = type.child
                    if child.type == TypeStruct:
                        self.usage = "output_ptr"
                    if (child.name in basic_types and child.name != "char") or (child.type == TypePointer and (child.child.type == TypeClass or child.child.type == TypeStruct)):
                        self.usage = "output"

    class ParsedMethod:
        def __init__(self, node):
            self.node = node
            self.name = node.name
            self.args = []
            self.library = "UK"
            self.struct = None


        def parse_arguments(self):
            arg_overrides = arg_usage_overrides.get(self.name, {})
            optionals = optional_arguments.get(self.name, {})

            for param in self.node.type.args.params:
                arg = MethodArgument(param)
                arg.usage = arg_overrides.get(arg.name, arg.usage)
                arg.optional = optionals.get(arg.name, arg.optional)
                self.args.append(arg)

        def detect_scope(self):
            first_arg = self.args[0]
            caps_name = self.name.upper()
            if first_arg != None:
                if first_arg.type.type == TypePointer and first_arg.type.child.type == TypeClass:
                    type_name = first_arg.type.child.name
                    if caps_name.startswith(type_name + "_"):
                        method_name = self.name[len(type_name) + 1:]
                        struct = structs[type_name]
                        self.struct = struct
                        struct.methods.append((to_snake_case(method_name), self))
                        if caps_name.startswith("FMOD_STUDIO_"):
                            self.library = "ST"
                        elif caps_name.startswith("FMOD_"):
                            self.library = "LL"
                        return

            method_name = self.name
            table_index = -2
            if caps_name.startswith("FMOD_STUDIO_"):
                table_index = -1
                self.library = "ST"
                method_name = self.name[len("FMOD_STUDIO_"):]
            elif caps_name.startswith("FMOD_"):
                self.library = "LL"
                method_name = self.name[len("FMOD_"):]
            global_functions.append((table_index, to_snake_case(method_name), self))

        def derive_template_data(self):
            self.generated = True
            arg_index = 1
            return_count = 0
            output_ptr_count = 0
            for arg in self.args:
                if arg.usage == "unknown":
                    self.generated = False
                if arg.usage == "input" or arg.usage == "input_ptr" or arg.usage == "input_deref":
                    arg.arg_index = arg_index
                    arg_index = arg_index + 1
                if arg.usage == "output":
                    return_count = return_count + 1
                if arg.usage == "output_ptr":
                    arg.output_ptr_index = output_ptr_count
                    arg.output_index = return_count
                    return_count = return_count + 1
                    output_ptr_count = output_ptr_count + 1
                arg.accessor = accessors[arg.usage] if arg.usage in accessors else ""
            self.return_count = return_count
            self.output_ptr_count = output_ptr_count
            self.refcount_release = (self.struct and self.struct.name in ref_counted and self.struct.name + "_RELEASE" == self.name.upper())
            if self.refcount_release:
                self.struct.release_name = self.name
            if not self.generated:
                print("Cannot auto-generate: " + self.name)

        def parse(self):
            self.parse_arguments()
            self.detect_scope()
            self.derive_template_data()

    def parse_struct(struct):
        if struct.name in exclusions:
            return
        if struct.decls == None:
            if struct.name not in structs:
                types[struct.name] = TypeClass
                parsed_class = ParsedStruct()
                parsed_class.make_class(struct.name)
                structs[struct.name] = parsed_class
        else:
            types[struct.name] = TypeStruct
            parsed_struct = structs[struct.name] if struct.name in structs else ParsedStruct()
            parsed_struct.parse_struct(struct)
            structs[struct.name] = parsed_struct

    basic_types["char"] = ParsedTypeDecl(c_type="char")
    basic_types["short"] = ParsedTypeDecl(c_type="short")
    basic_types["int"] = ParsedTypeDecl(c_type="int")
    basic_types["long_long"] = ParsedTypeDecl(c_type="long long")
    basic_types["unsigned_char"] = ParsedTypeDecl(c_type="unsigned char")
    basic_types["unsigned_short"] = ParsedTypeDecl(c_type="unsigned short")
    basic_types["unsigned_int"] = ParsedTypeDecl(c_type="unsigned int")
    basic_types["unsigned_long_long"] = ParsedTypeDecl(c_type="unsigned long long")
    basic_types["FMOD_BOOL"] = ParsedTypeDecl(c_type="FMOD_BOOL")
    basic_types["float"] = ParsedTypeDecl(c_type="float")
    basic_types["double"] = ParsedTypeDecl(c_type="double")
    basic_types["ptr_char"] = ParsedTypeDecl(name="ptr_char", c_type="char*", writeable=False, type=TypePointer)
    basic_types["FMOD_VECTOR"] = ParsedTypeDecl(c_type="FMOD_VECTOR")

    int_type = basic_types["int"]

    for key in basic_types.keys():
        types[key] = TypeBasic

    for node in ast:
        if isinstance(node, c_ast.Typedef):
            if isinstance(node.type, c_ast.TypeDecl):
                if isinstance(node.type.type, c_ast.Enum):
                    types[node.name] = TypeBasic
                    basic_types[node.name] = ParsedTypeDecl(c_type=node.name)
                    enum_types.append(node.name)
                    for enumlist in node.type.type:
                        for enum in enumlist:
                            if re.search("_FORCEINT$", enum.name) == None:
                                enums.append(re.sub("^FMOD_", "", enum.name))

                elif isinstance(node.type.type, c_ast.Struct):
                    parse_struct(node.type.type)

                elif node.name not in basic_types:
                    parsed_type = ParsedTypeDecl(node=node.type)
                    if parsed_type.name in basic_types:
                        types[node.name] = TypeBasic
                        basic_types[node.name] = basic_types[parsed_type.name]
                    else:
                        print("Unknown typedef")
                        node.show()

        elif isinstance(node, c_ast.Decl):
            if isinstance(node.type, c_ast.Struct):
                parse_struct(node.type)

            elif isinstance(node.type, c_ast.FuncDecl):
                functions.append(ParsedMethod(node))

            else:
                print("Unknown declaration")
                node.show()

        else:
            node.show()

    add_defined_enums(enums)

    for function in functions:
        function.parse()

    env = Environment(
        loader = FileSystemLoader('.'),
        autoescape = False,
    )
    template = env.get_template('fmod_generated_template.c')

    output = template.render(
        enums = enums,
        structs = list(structs.values()),
        functions = functions,
        global_functions = global_functions,
        enum_types = enum_types,
    )

    with open('src/fmod_generated.c', 'w') as f:
        f.write(output)


if __name__ == "__main__":
    filename = 'include/fmod_studio.h'

    ast = parse_file(filename, use_cpp=True,
            cpp_path='gcc',
            cpp_args=['-E'])

    generate_bindings(ast)
    # ast.show()
