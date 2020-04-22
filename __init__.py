from binaryninja import *
import os
import getpass

bitsize = 32 
max_len = 512

afl_fuzzer_func  =  "int main(int argc, char *argv[])"
afl_fuzzer_loop = "do"
afl_fuzzer_loop_end = "while(Size > 0);"
afl_fuzzer_loop_init = """
    int BufSize = {max_len};
    char Buf[BufSize];
    char* Data = NULL;
    int Size = BufSize;
""".format(max_len=max_len)

afl_fuzzer_loop_load = """
    /* Reset state. */
    memset(Buf, 0, BufSize);

    /* Read input data. */
    Size = read(0, Buf, BufSize);
    Data = &Buf[0];
""".format(max_len=max_len)


fuzzer_func = afl_fuzzer_func
fuzzer_loop = afl_fuzzer_loop
fuzzer_loop_init = afl_fuzzer_loop_init
fuzzer_loop_load = afl_fuzzer_loop_load
fuzzer_loop_end = afl_fuzzer_loop_end

template = """#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <dlfcn.h>
#include <unistd.h> 

int isLoaded = 0;
void *{libname} = NULL;

{typedefs}

{globaldefs}

void CloseLibrary()
{{
    if({libname}){{
        dlclose({libname});
        {libname} = NULL;
    }}
    return;
}}

int LoadLibrary()
{{
    {libname} = dlopen("{lib}", RTLD_NOW|RTLD_GLOBAL);
    fprintf(stderr, "%s\\n", dlerror());
    printf("loaded {libname} at %p\\n", {libname});
    atexit(CloseLibrary);
    return {libname} != NULL;
}}

void ResolveSymbols()
{{
    {dlsyms} 
}}

{fuzzer_func}
{{
    {fuzzer_loop_init}

    if (!isLoaded)
    {{
        if(!LoadLibrary()) 
        {{
            printf("could not load {libname}.so\\n");
            return -1;
        }}
        ResolveSymbols();
        isLoaded = 1;
    }}

    if (Size==0)
    {{
        return 0;
    }}

    uint8_t choice = 0;
    {fuzzer_loop_load}

    {fuzzer_loop}
    {{
        if (Size < sizeof(choice)) 
        {{
            break;
        }}

        choice = Data[0];
        Data += sizeof(choice);
        Size -= sizeof(choice);

        switch(choice%{number}) 
        {{
            {choices}
        }}
    }}{fuzzer_loop_end}
    return 0;
}}
"""

class parse_func():
    def __init__(self, function):
        self._name = function.name
        self._type = function.function_type
        self._argument_types = convert_function_parameter_types(function.function_type.parameters)

    def typedef(self):
        argument_types = convert_function_parameter_types(self._type.parameters)
        ret = binja_type_to_c_type(str(self._type.return_value))

        return "typedef %s(*%s_t)(%s);" % (ret, self._name, ",".join(argument_types))

    def resolve(self, libname):
        return self.dlsym(libname) +" " + self.printer()

    def dlsym(self, libname):
        return ("%s = (%s_t)dlsym({libname}, \"%s\");" % (self._name, self._name, self._name)).format(libname=libname)

    def printer(self):
        return "printf(\"loaded %s at %%p\\n\", %s);" % (self._name, self._name)

    def globaldef(self):
        return "%s_t %s = NULL;" % (self._name, self._name)

    def choice(self, number):
        localVars=[]
        args = []
        frees = []
        minbufsize=0
        idx = ["0"];
        lvaridx = number*1000
        # (int, int, int, int*)
        # (char *, int, int)
        
        for parameter in self._type.parameters:
            if not ("*" in str(parameter.type)):
                tmp_type = binja_type_to_c_type(str(parameter.type))
                tmp_type_size = get_c_type_byte_size(tmp_type)
                localVars.append("""
                if(Size < 4){
                    // not enough bytes in buffer
                    return 0;
                }
                %s l_%s; memcpy(&l_%s, Data+(%s), sizeof(%s));""" %(tmp_type, lvaridx, lvaridx, " + ".join(idx), tmp_type))
                args.append("l_%s" % lvaridx)
                lvaridx += 1

                minbufsize += tmp_type_size
                idx.append(str(tmp_type_size))
            else:
                localVars.append("""
                if(Size < 4){
                    //not enough bytes in buffer
                    return 0;
                }
                unsigned int strlen_%s; memcpy(&strlen_%s, Data+(%s), sizeof(int));
                if(strlen_%s > Size){
                    //not enough bytes in buffer
                    return 0;
                }
                char *tmpbuf_%s = malloc(strlen_%s+1);
                if(tmpbuf_%s == NULL){
                    //could not allocate tmpstring
                    return 0;
                }
                strncpy(tmpbuf_%s, Data+(%s)+4, strlen_%s);
                tmpbuf_%s[strlen_%s] = 0;
                """ % (lvaridx, lvaridx, " + ".join(idx), lvaridx, lvaridx, lvaridx, lvaridx, lvaridx, " + ".join(idx), lvaridx, lvaridx, lvaridx))
                args.append("tmpbuf_%s" % lvaridx)
                frees.append("free(tmpbuf_%s);" %(lvaridx));
                minbufsize += 4
                idx.append("4")
                idx.append("strlen_%s" % (lvaridx))
                lvaridx += 1

        return """
            case {number}:
                {localVars}
                {function}({args});
                Data += ({idx});
                Size -= ({idx});
                {frees}
                break;""".format(idx=" + ".join(idx), localVars=" ".join(localVars), number=number, function=self._name, args=", ".join(args), frees=" ".join(frees))

def get_c_type_byte_size(c_type):
    mapping = {"long long int":8, "int":4, "unsigned int":4, "void":4, "short":2, "char":1}
    
    if not (c_type in mapping):
        raise Exception("Unknown type '%s'" % c_type)
    
    return mapping[c_type]

def binja_type_to_c_type(binja_type):
    if len(binja_type.split()) > 1:
        return " ".join( map(binja_type_to_c_type, binja_type.split()))
    mapping = {"uint64_t":"long long int", "int64_t":"long long int", "int32_t":"int", "uint32_t":"unsigned int", "void":"void", "char":"char", "int16_t":"short int", "const":"const" }

    has_pointer = ""
    if "*" in binja_type:
        has_pointer = "*"
    tmp_type = binja_type.replace("*", "")
    if not (tmp_type in mapping):
        raise Exception("Unknown type '%s'" % binja_type)
    return "%s%s" % (mapping[tmp_type], has_pointer)

def convert_function_parameter_types(function_parameters):
    function_types = []
    for parameter in function_parameters:
        function_types.append(binja_type_to_c_type(str(parameter.type)))
    return function_types      

def get_type_for_function(function):
    filtered_funcs = [
            "_start", 
            "_init", 
            "_fini", 
            "__stack_chk_fail_local", 
            "__stack_chk_fail", 
            "__cxa_finalize",
            "deregister_tm_clones",
            "register_tm_clones",
            "__do_global_dtors_aux",
            "frame_dummy",
            "__x86.get_pc_thunk.ax",
            "__x86.get_pc_thunk.dx",
            "__gmon_start__",
    ]

    if function.name in filtered_funcs:
        return

    return parse_func(function)

def get_types(bv):
    types = []
    functions = [bv.get_function_at(sym.address) for sym in bv.get_symbols_of_type(SymbolType.FunctionSymbol)]
    for function in functions:
        tmp = get_type_for_function(function)
        if not tmp:
            continue
        types.append(tmp)
    return types

def write_template(lib, f_types):
    name_field = SaveFileNameField("Save to")
    if get_form_input([name_field], "Fuzzit"):
        if name_field.result == '':
            return
        with open(name_field.result, 'w+') as f:
            libname = lib.split(".so")[0]
            f.write(template.format(
                    lib = lib,
                    libname=libname,
                    max_len=max_len,
                    fuzzer_loop=fuzzer_loop,
                    fuzzer_loop_end=fuzzer_loop_end,
                    fuzzer_loop_init=fuzzer_loop_init,
                    fuzzer_loop_load=fuzzer_loop_load,
                    fuzzer_func=fuzzer_func,
                    typedefs   ="\n".join([a.typedef() for a in f_types]), 
                    dlsyms     ="\n    ".join([a.resolve(libname) for a in f_types]),
                    globaldefs ="\n".join([a.globaldef() for a in f_types]),
                    choices    ="\n".join([a.choice(i) for i, a in enumerate(f_types)]),
                    number = str(len(f_types))))
    else:
        return


def create_for_function(bv, func):
    lib = os.path.basename(bv.file.original_filename)
    f_type = get_type_for_function(func)
    if len(f_type) == None:
        print("No usable functions found")
        return
    write_template(lib, [f_type])
    
def create(bv):
    lib = os.path.basename(bv.file.original_filename)
    f_types = get_types(bv)
    if len(f_types) == 0:
        print("No usable functions found")
        return
    write_template(lib, f_types)
   
PluginCommand.register("Fuzzit\\Create test harness", "Attempt to create a test harness for exported functions of this shared library", create)
PluginCommand.register_for_function("Fuzzit\\Create test harness for function", "Attempt to create a test harness for the selected function", create_for_function)
