"""Just-in-time compiling backend."""

from __future__ import division

__copyright__ = "Copyright (C) 2008 Andreas Kloeckner"

__license__ = """
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see U{http://www.gnu.org/licenses/}.
"""



from pytools import memoize_method




class JitLifter:
    def __init__(self, discr):
        self.discr = discr

    @memoize_method
    def make_lift(self, fgroup, with_scale, dtype):
        discr = self.discr
        from codepy.cgen import (
                FunctionDeclaration, FunctionBody, Typedef,
                Const, Reference, Value, POD,
                Statement, Include, Line, Block, Initializer, Assign,
                For, If,
                Define)

        from pytools import to_uncomplex_dtype

        from codepy.bpl import BoostPythonModule
        mod = BoostPythonModule()

        S = Statement
        mod.add_to_preamble([
            Include("hedge/face_operators.hpp"),
            Include("hedge/volume_operators.hpp"),
            Include("boost/foreach.hpp"),
            ])

        mod.add_to_module([
            S("namespace ublas = boost::numeric::ublas"),
            S("using namespace hedge"),
            S("using namespace pyublas"),
            Line(),
            Define("DOFS_PER_EL", fgroup.ldis_loc.node_count()),
            Define("FACES_PER_EL", fgroup.ldis_loc.face_count()),
            Define("DIMENSIONS", discr.dimensions),
            Line(),
            Typedef(POD(dtype, "value_type")),
            Typedef(POD(to_uncomplex_dtype(dtype), "uncomplex_type")),
            ])

        def if_(cond, result, else_=None):
            if cond:
                return [result]
            else:
                if else_ is None:
                    return []
                else:
                    return [else_]

        fdecl = FunctionDeclaration(
                    Value("void", "lift"),
                    [
                    Const(Reference(Value("face_group<face_pair<straight_face> >", "fg"))),
                    Value("ublas::matrix<uncomplex_type>", "matrix"),
                    Value("numpy_array<value_type>", "field"),
                    Value("numpy_array<value_type>", "result")
                    ]+if_(with_scale,
                        Const(Reference(Value("numpy_array<double>",
                            "elwise_post_scaling"))))
                    )

        def make_it(name, is_const=True, tpname="value_type"):
            if is_const:
                const = "const_"
            else:
                const = ""

            return Initializer(
                Value("numpy_array<%s>::%siterator" % (tpname, const), name+"_it"),
                "%s.begin()" % name)

        fbody = Block([
            make_it("field"),
            make_it("result", is_const=False),
            ]+if_(with_scale, make_it("elwise_post_scaling", tpname="double"))+[
            Line(),
            For("unsigned fg_el_nr = 0",
                "fg_el_nr < fg.element_count()",
                "++fg_el_nr",
                Block([
                    Initializer(
                        Value("node_number_t", "dest_el_base"),
                        "fg.local_el_write_base[fg_el_nr]"),
                    Initializer(
                        Value("node_number_t", "src_el_base"),
                        "FACES_PER_EL*fg.face_length()*fg_el_nr"),
                    Line(),
                    For("unsigned i = 0",
                        "i < DOFS_PER_EL",
                        "++i",
                        Block([
                            Initializer(Value("value_type", "tmp"), 0),
                            Line(),
                            For("unsigned j = 0",
                                "j < FACES_PER_EL*fg.face_length()",
                                "++j",
                                S("tmp += matrix(i, j)*field_it[src_el_base+j]")
                                ),
                            Line(),
                            ]+if_(with_scale,
                                Assign("result_it[dest_el_base+i]",
                                    "tmp * value_type(*elwise_post_scaling_it)"),
                                Assign("result_it[dest_el_base+i]", "tmp"))
                            )
                        ),
                    ]+if_(with_scale, S("elwise_post_scaling_it++"))
                    )
                )
            ])

        mod.add_function(FunctionBody(fdecl, fbody))

        #print "----------------------------------------------------------------"
        #print FunctionBody(fdecl, fbody)
        #raw_input()

        return mod.compile(self.discr.toolchain).lift

    def __call__(self, fgroup, matrix, scaling, field, out):
        result = self.discr.volume_zeros(dtype=field.dtype)

        from pytools import to_uncomplex_dtype
        uncomplex_dtype = to_uncomplex_dtype(field.dtype)
        args = [fgroup, matrix.astype(uncomplex_dtype), field, out]

        if scaling is not None:
            args.append(scaling)

        self.make_lift(fgroup, 
                with_scale=scaling is not None, 
                dtype=field.dtype)(*args)
