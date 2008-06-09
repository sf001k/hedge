"""Interface with Nvidia CUDA."""

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



import numpy
import numpy.linalg as la
from pytools import memoize_method, memoize
import hedge.optemplate
import pycuda.driver as cuda
import pycuda.gpuarray as gpuarray
import pymbolic.mapper.stringifier




# structures ------------------------------------------------------------------
@memoize
def flux_header_struct():
    from hedge.cuda.cgen import Struct, POD

    return Struct("flux_header", [
        POD(numpy.uint16, "els_in_block"),
        POD(numpy.uint16, "same_facepairs_end"),
        POD(numpy.uint16, "diff_facepairs_end"),
        POD(numpy.uint16, "bdry_facepairs_end"),
        ])

@memoize
def face_pair_struct(float_type, dims):
    from hedge.cuda.cgen import Struct, POD, ArrayOf
    return Struct("face_pair", [
        POD(float_type, "h", ),
        POD(float_type, "order"),
        POD(float_type, "face_jacobian"),
        ArrayOf(POD(float_type, "normal"), dims),

        POD(numpy.uint32, "a_base"),
        POD(numpy.uint32, "b_base"),

        POD(numpy.uint16, "a_ilist_index"),
        POD(numpy.uint16, "b_ilist_index"), 
        POD(numpy.uint16, "b_write_ilist_index"), 
        POD(numpy.uint8, "a_flux_number"),
        POD(numpy.uint8, "b_flux_number_and_bdry_flag"), 
        POD(numpy.uint16, "a_dest"), 
        POD(numpy.uint16, "b_dest"), 
        ])



# flux to code mapper ---------------------------------------------------------
class FluxToCodeMapper2(pymbolic.mapper.stringifier.StringifyMapper):
    def __init__(self, flip_normal):
        def float_mapper(x):
            if isinstance(x, float):
                return "%sf" % repr(x)
            else:
                return repr(x)

        pymbolic.mapper.stringifier.StringifyMapper.__init__(self, float_mapper)
        self.flip_normal = flip_normal

    def map_normal(self, expr, enclosing_prec):
        if self.flip_normal:
            sign = "-"
        else:
            sign = ""
        return "%sfpair->normal[%d]" % (sign, expr.axis)

    def map_penalty_term(self, expr, enclosing_prec):
        return ("pow(fpair->order*fpair->order/fpair->h, %r)" 
                % expr.power)

    def map_if_positive(self, expr, enclosing_prec):
        from pymbolic.mapper.stringifier import PREC_NONE
        return "(%s > 0 ? %s : %s)" % (
                self.rec(expr.criterion, PREC_NONE),
                self.rec(expr.then, PREC_NONE),
                self.rec(expr.else_, PREC_NONE),
                )




# exec mapper -----------------------------------------------------------------
class ExecutionMapper(hedge.optemplate.Evaluator,
        hedge.optemplate.BoundOpMapperMixin, 
        hedge.optemplate.LocalOpReducerMixin):

    def __init__(self, context, executor):
        hedge.optemplate.Evaluator.__init__(self, context)
        self.ex = executor

        self.diff_xyz_cache = {}

    def print_error_structure(self, computed, reference, diff):
        discr = self.ex.discr

        norm_ref = la.norm(reference)
        struc = ""

        numpy.set_printoptions(precision=2, linewidth=130, suppress=True)
        for block in discr.blocks:
            i_el = 0
            for mb in block.microblocks:
                for el in mb:
                    s = discr.find_el_range(el.id)
                    relerr = la.norm(diff[s])/norm_ref
                    if relerr > 1e-4:
                        struc += "*"
                        if True:
                            print "block %d, el %d, global el #%d, rel.l2err=%g" % (
                                    block.number, i_el, el.id, relerr)
                            print computed[s]
                            print reference[s]
                            print diff[s]
                            raw_input()
                    elif numpy.isnan(relerr):
                        struc += "N"
                        if False:
                            print "block %d, el %d, global el #%d, rel.l2err=%g" % (
                                    block.number, i_el, el.id, relerr)
                            print computed[s]
                            print reference[s]
                            print diff[s]
                            raw_input()
                    else:
                        if numpy.max(numpy.abs(reference[s])) == 0:
                            struc += "0"
                        else:
                            if False:
                                print "block %d, el %d, global el #%d, rel.l2err=%g" % (
                                        block.number, i_el, el.id, relerr)
                                print computed[s]
                                print reference[s]
                                print diff[s]
                                raw_input()
                            struc += "."
                    i_el += 1
                struc += " "
            struc += "\n"
        print
        print struc

    def map_diff_base(self, op, field_expr, out=None):
        try:
            xyz_diff = self.diff_xyz_cache[op.__class__, field_expr]
        except KeyError:
            pass
        else:
            print "HIT"
            return xyz_diff[op.xyz_axis]

        discr = self.ex.discr
        d = discr.dimensions

        eg, = discr.element_groups
        func, texrefs, field_texref = self.ex.get_diff_kernel(op.__class__, eg)

        fplan = discr.flux_plan
        lplan = fplan.localop_plan()

        field = self.rec(field_expr)
        assert field.dtype == discr.flux_plan.float_type

        field_texref.set_address(
                field.gpudata, field.size*field.dtype.itemsize)
        
        from hedge.cuda.tools import int_ceiling
        kwargs = {
                "block": (lplan.chunk_size, lplan.parallelism.p, 1),
                "grid": (fplan.mb_chunks, 
                    int_ceiling(
                        fplan.dofs_per_block()*len(discr.blocks)/
                        lplan.dofs_per_macroblock())
                    ),
                "time_kernel": discr.instrumented,
                "texrefs": texrefs,
                }

        #debugbuf = gpuarray.zeros((512,), dtype=numpy.float32)

        xyz_diff = [discr.volume_empty() for axis in range(d)]
        elgroup, = discr.element_groups
        args = xyz_diff+[
                self.ex.gpu_diffmats(op.__class__, eg).device_memory,
                #debugbuf,
                ]

        kernel_time = func(*args, **kwargs)
        if discr.instrumented:
            discr.diff_op_timer.add_time(kernel_time)
            discr.diff_op_counter.add(discr.dimensions)

        if False:
            copied_debugbuf = debugbuf.get()
            print "DEBUG"
            #print numpy.reshape(copied_debugbuf, (len(copied_debugbuf)//16, 16))
            print copied_debugbuf[:100].reshape((10,10))
            raw_input()
        
        if discr.debug:
            f = discr.volume_from_gpu(field)
            dx = discr.volume_from_gpu(xyz_diff[0])
            
            test_discr = discr.test_discr
            real_dx = test_discr.nabla[0].apply(f.astype(numpy.float64))
            
            diff = dx - real_dx
            #self.print_error_structure(dx, real_dx, diff)
            #raw_input()

            rel_err_norm = la.norm(diff)/la.norm(real_dx)
            print rel_err_norm
            assert rel_err_norm < 5e-5

        self.diff_xyz_cache[op.__class__, field_expr] = xyz_diff
        return xyz_diff[op.xyz_axis]

    def map_whole_domain_flux(self, op, field_expr, out=None):
        field = self.rec(field_expr)
        discr = self.ex.discr

        eg, = discr.element_groups
        fdata = self.ex.flux_with_temp_data(op, eg)
        func, texrefs, field_texref, bfield_texref = \
                self.ex.get_flux_with_temp_kernel(op)

        flux_par = discr.flux_plan.parallelism
        
        kwargs = {
                "texrefs": texrefs, 
                "block": (discr.flux_plan.mb_aligned_floats, flux_par.p, 1),
                "grid": (len(discr.blocks), 1),
                "time_kernel": discr.instrumented,
                }

        flux = discr.volume_empty() 
        bfield = None
        for boundary in op.boundaries:
            if bfield is None:
                bfield = self.rec(boundary.bfield_expr)
            else:
                bfield = bfield + self.rec(boundary.bfield_expr)
            
        assert field.dtype == discr.flux_plan.float_type
        assert bfield.dtype == discr.flux_plan.float_type

        debugbuf = gpuarray.zeros((512,), dtype=numpy.float32)

        args = [
                debugbuf, 
                flux, 
                #field, bfield, 
                fdata.device_memory,
                self.ex.index_list_global_data().device_memory,
                ]

        field_texref.set_address(
                field.gpudata, field.size*field.dtype.itemsize)
        bfield_texref.set_address(
                bfield.gpudata, bfield.size*field.dtype.itemsize)

        kernel_time = func(*args, **kwargs)
        if discr.instrumented:
            discr.inner_flux_timer.add_time(kernel_time)
            discr.inner_flux_counter.add()

        if False:
            copied_debugbuf = debugbuf.get()
            print "DEBUG"
            numpy.set_printoptions(linewidth=100)
            print numpy.reshape(copied_debugbuf, (32, 16))
            #print copied_debugbuf
            raw_input()

        if discr.debug:
            cot = discr.test_discr.compile(op.flux_optemplate)
            ctx = {field_expr.name: 
                    discr.volume_from_gpu(field).astype(numpy.float64)
                    }
            for boundary in op.boundaries:
                ctx[boundary.bfield_expr.name] = \
                        discr.test_discr.boundary_zeros(boundary.tag)
            true_flux = cot(**ctx)
            
            copied_flux = discr.volume_from_gpu(flux)

            diff = copied_flux-true_flux

            norm_true = la.norm(true_flux)

            if False:
                self.print_error_structure(copied_flux, true_flux, diff)
                raw_input()

            print la.norm(diff)/norm_true
            assert la.norm(diff)/norm_true < 1e-6

        if False:
            copied_bfield = bfield.get()
            face_len = discr.flux_plan.ldis.face_node_count()
            aligned_face_len = discr.devdata.align_dtype(face_len, 4)
            for elface in discr.mesh.tag_to_boundary.get('inflow', []):
                face_stor = discr.face_storage_map[elface]
                bdry_stor = face_stor.opposite
                gpu_base = bdry_stor.gpu_bdry_index_in_floats
                print gpu_base, copied_bfield[gpu_base:gpu_base+aligned_face_len]
                raw_input()

        return flux




class OpTemplateWithEnvironment(object):
    def __init__(self, discr, optemplate):
        self.discr = discr

        from hedge.optemplate import OperatorBinder, InverseMassContractor, \
                FluxDecomposer
        from pymbolic.mapper.constant_folder import CommutativeConstantFoldingMapper
        from hedge.cuda.optemplate import BoundaryCombiner

        self.optemplate = (
                BoundaryCombiner(discr)(
                    InverseMassContractor()(
                        CommutativeConstantFoldingMapper()(
                            FluxDecomposer()(
                                OperatorBinder()(
                                    optemplate))))))

    def __call__(self, **vars):
        return ExecutionMapper(vars, self)(self.optemplate)




    # diff kernel -------------------------------------------------------------
    def get_load_code(self, dest, base, bytes, word_type=numpy.uint32,
            descr=None):
        from hedge.cuda.cgen import \
                Pointer, POD, Value, ArrayOf, Const, \
                Comment, Block, Line, \
                Constant, Initializer, If, For, Statement, Assign

        from hedge.cuda.cgen import dtype_to_ctype
        copy_dtype = numpy.dtype(word_type)
        copy_dtype_str = dtype_to_ctype(copy_dtype)

        code = []
        if descr is not None:
            code.append(Comment(descr))

        code.extend([
            Block([
                Constant(Pointer(POD(copy_dtype, "load_base")), 
                    ("(%s *) (%s)" % (copy_dtype_str, base))),
                For("unsigned word_nr = THREAD_NUM", 
                    "word_nr*sizeof(int) < (%s)" % bytes, 
                    "word_nr += COALESCING_THREAD_COUNT",
                    Statement("((%s *) (%s))[word_nr] = load_base[word_nr]"
                        % (copy_dtype_str, dest))
                    ),
                ]),
            Line(),
            ])

        return code

    @memoize_method
    def get_diff_kernel(self, diff_op_cls, elgroup):
        from hedge.cuda.cgen import \
                Pointer, POD, Value, ArrayOf, Const, \
                Module, FunctionDeclaration, FunctionBody, Block, \
                Comment, Line, \
                CudaShared, CudaGlobal, Static, \
                Define, \
                Constant, Initializer, If, For, Statement, Assign
                
        discr = self.discr
        d = discr.dimensions
        dims = range(d)
        fplan = discr.flux_plan
        lplan = fplan.localop_plan()

        lop_par = lplan.parallelism
        diffmat_data = self.gpu_diffmats(diff_op_cls, elgroup)
        elgroup, = discr.element_groups

        float_type = fplan.float_type

        f_decl = CudaGlobal(FunctionDeclaration(Value("void", "apply_diff_mat"), 
            [Pointer(POD(float_type, "dxyz%d" % i)) for i in dims]
            + [
                Pointer(POD(numpy.uint8, "gmem_diff_rst_mat")),
                #Pointer(POD(float_type, "debugbuf")),
                ]
            ))

        rst_channels = discr.devdata.make_valid_tex_channel_count(d)
        cmod = Module([
                Value("texture<float%d, 2, cudaReadModeElementType>"
                    % rst_channels, 
                    "rst_to_xyz_tex"),
                Value("texture<float, 1, cudaReadModeElementType>", 
                    "field_tex"),
                Line(),
                Define("DIMENSIONS", discr.dimensions),
                Define("DOFS_PER_EL", fplan.dofs_per_el()),
                Line(),
                Define("CHUNK_DOF", "threadIdx.x"),
                Define("PAR_MB_NR", "threadIdx.y"),
                Line(),
                Define("MB_CHUNK", "blockIdx.x"),
                Define("MACROBLOCK_NR", "blockIdx.y"),
                Line(),
                Define("CHUNK_DOF_COUNT", lplan.chunk_size),
                Define("MB_CHUNK_COUNT", fplan.mb_chunks),
                Define("MB_DOF_COUNT", "(MB_CHUNK_COUNT*CHUNK_DOF_COUNT)"),
                Define("MB_EL_COUNT", fplan.mb_elements),
                Define("PAR_MB_COUNT", lplan.parallelism.p),
                Define("SEQ_MB_COUNT", lplan.parallelism.s),
                Line(),
                Define("THREAD_NUM", "(CHUNK_DOF+PAR_MB_NR*CHUNK_DOF_COUNT)"),
                Define("COALESCING_THREAD_COUNT", "(PAR_MB_COUNT*CHUNK_DOF_COUNT)"),
                Line(),
                Define("MB_DOF_BASE", "(MB_CHUNK*CHUNK_DOF_COUNT)"),
                Define("MB_DOF", "(MB_DOF_BASE+CHUNK_DOF)"),
                Define("GLOBAL_MB_NR_BASE", "(MACROBLOCK_NR*PAR_MB_COUNT*SEQ_MB_COUNT)"),
                Line(),
                Define("DIFF_MAT_BLOCK_BYTES", diffmat_data.block_bytes),

                Line(),
                CudaShared(ArrayOf(POD(float_type, "smem_diff_rst_mat"), 
                    "DIMENSIONS*DOFS_PER_EL*CHUNK_DOF_COUNT")),
                Line(),
                ])

        S = Statement
        f_body = Block()
            
        f_body.extend_log_block("calculate responsibility data", [
            Initializer(POD(numpy.uint8, "mb_el"),
                "MB_DOF/DOFS_PER_EL"),
            ])

        f_body.extend(
            self.get_load_code(
                dest="smem_diff_rst_mat",
                base=("gmem_diff_rst_mat + MB_CHUNK*DIFF_MAT_BLOCK_BYTES"),
                bytes="CHUNK_DOF_COUNT*DIMENSIONS*DOFS_PER_EL*%d" % fplan.float_size,
                descr="load diff mat chunk"))


        # ---------------------------------------------------------------------
        def get_scalar_diff_code(matrix_row, dest_pattern):
            code = []
            for axis in dims:
                code.append(
                    Initializer(POD(float_type, "drst%d" % axis), 0))

            code.append(Line())

            def get_mat_entry(row, col, axis):
                return ("smem_diff_rst_mat["
                        "(%(row)s * DIMENSIONS + %(axis)s)*DOFS_PER_EL"
                        "+%(col)s"
                        "]" % {"row":row, "col":col, "axis":axis}
                        )

            tex_channels = ["x", "y", "z", "w"]
            from pytools import flatten
            code.extend(
                    [POD(float_type, "field_value"),
                        Line(),
                        ]
                    +list(flatten( [
                        Assign("field_value", 
                            #"int_dofs[PAR_MB_NR][chunk_el*DOFS_PER_EL+%d]" % (j)
                            "tex1Dfetch(field_tex, "
                            "global_mb_dof_base"
                            "+mb_el*DOFS_PER_EL+%d)" % j
                            ),
                        Line(),
                        ]
                        +[
                        S("drst%d += %s * field_value" 
                            % (axis, get_mat_entry(matrix_row, j, axis)))
                        for axis in dims
                        ]+[Line()]
                        for j in range(fplan.dofs_per_el())
                        ))
                    )

            for glob_axis in dims:
                code.append(Block([
                    Initializer(Value("float%d" % rst_channels, "rst_to_xyz"),
                        "tex2D(rst_to_xyz_tex, %d, global_mb_nr*MB_EL_COUNT+mb_el)" % glob_axis
                        ),
                    Assign(
                        dest_pattern % glob_axis,
                        " + ".join(
                            "rst_to_xyz.%s"
                            "*"
                            "drst%d" % (tex_channels[loc_axis], loc_axis)
                            for loc_axis in dims
                            )
                        )
                    ]))
            return code

        f_body.extend([
            For("unsigned short seq_mb_number = 0",
                "seq_mb_number < SEQ_MB_COUNT",
                "++seq_mb_number",
                Block([
                    Initializer(POD(numpy.uint32, "global_mb_nr"),
                        "GLOBAL_MB_NR_BASE + seq_mb_number*PAR_MB_COUNT + PAR_MB_NR"),
                    Initializer(POD(numpy.uint32, "global_mb_dof_base"),
                        "global_mb_nr*MB_DOF_COUNT"),
                    Line(),
                    #Comment("load dofs"),
                    #For("unsigned short load_dof = CHUNK_DOF",
                        #"load_dof < chunk_load_dof_count",
                        #"load_dof += CHUNK_DOF_COUNT",
                        #Assign("int_dofs[PAR_MB_NR][load_dof]",
                            #"tex1Dfetch(field_tex, global_mb_dof_base+chunk_start_load_dof+load_dof)")
                        #),
                    ##Line(),
                    #Line(),
                    #S("__syncthreads()"),
                    #Line(),
                    ]+
                    get_scalar_diff_code(
                        "CHUNK_DOF",
                        "dxyz%d[global_mb_dof_base+MB_DOF]")
                    )
                )
            ])

        # finish off ----------------------------------------------------------
        cmod.append(FunctionBody(f_decl, f_body))

        mod = cuda.SourceModule(cmod, 
                keep=True, 
                #options=["--maxrregcount=10"]
                )
        print "lmem=%d smem=%d regs=%d" % (mod.lmem, mod.smem, mod.registers)

        rst_to_xyz_texref = mod.get_texref("rst_to_xyz_tex")
        cuda.bind_array_to_texref(
                self.localop_rst_to_xyz(diff_op_cls, elgroup), 
                rst_to_xyz_texref)

        field_texref = mod.get_texref("field_tex")
        texrefs = [field_texref, rst_to_xyz_texref]

        return mod.get_function("apply_diff_mat"), texrefs, field_texref




    # flux kernel -------------------------------------------------------------
    @memoize_method
    def get_flux_with_temp_kernel(self, wdflux):
        from hedge.cuda.cgen import \
                Pointer, POD, Value, ArrayOf, Const, \
                Module, FunctionDeclaration, FunctionBody, Block, \
                Comment, Line, \
                CudaShared, CudaGlobal, Static, \
                Define, Pragma, \
                Constant, Initializer, If, For, Statement, Assign, While
                
        discr = self.discr
        fplan = discr.flux_plan
        d = discr.dimensions
        dims = range(d)

        flux_par = fplan.parallelism
        elgroup, = discr.element_groups
        flux_with_temp_data = self.flux_with_temp_data(wdflux, elgroup)

        float_type = fplan.float_type

        f_decl = CudaGlobal(FunctionDeclaration(Value("void", "apply_flux"), 
            [
                Pointer(POD(float_type, "debugbuf")),
                Pointer(POD(float_type, "flux")),
                Pointer(POD(numpy.uint8, "gmem_data")),
                Pointer(POD(numpy.uint8, "gmem_index_lists")),
                ]
            ))

        cmod = Module([
                Value("texture<float, 2, cudaReadModeElementType>", 
                    "lift_matrix_tex"),
                Value("texture<float, 1, cudaReadModeElementType>", 
                    "field_tex"),
                Value("texture<float, 1, cudaReadModeElementType>", 
                    "bfield_tex"),
                flux_header_struct(),
                face_pair_struct(float_type, discr.dimensions),
                Line(),
                Define("DIMENSIONS", discr.dimensions),
                Define("DOFS_PER_EL", fplan.dofs_per_el()),
                Line(),
                Define("MB_DOF", "threadIdx.x"),
                Define("PAR_MB_NR", "threadIdx.y"),
                Line(),
                Define("MB_EL_COUNT", fplan.mb_elements),
                Define("MB_DOF_COUNT", fplan.mb_aligned_floats),
                Define("PAR_MB_COUNT", fplan.parallelism.p),
                Define("SER_MB_COUNT", fplan.parallelism.s),
                Define("BLOCK_MB_COUNT", "(PAR_MB_COUNT*SER_MB_COUNT)"),
                Line(),
                Define("THREAD_NUM", "(PAR_MB_NR*MB_DOF_COUNT + MB_DOF)"),
                Define("THREAD_COUNT", "(MB_DOF_COUNT*PAR_MB_COUNT)"),
                Define("COALESCING_THREAD_COUNT", "(THREAD_COUNT & ~0xf)"),
                Line(),
                #Define("EL_DOF", "threadIdx.x"),
                #Define("BLOCK_EL", "threadIdx.y"),
                #Define("CONCURRENT_ELS", flux_par.p),
                #Define("INT_DOF_COUNT", discr.int_dof_count),
                Define("DOFS_BLOCK_BASE", "(blockIdx.x*BLOCK_MB_COUNT*MB_DOF_COUNT)"),
                Define("DATA_BLOCK_SIZE", flux_with_temp_data.block_bytes),
                Define("BASE_EL", "(base_mb*MB_EL_COUNT+mb_el)"),
                Line(),
                Comment("face-related stuff"),
                Define("DOFS_PER_FACE", fplan.dofs_per_face()),
                Define("FACES_PER_EL", fplan.faces_per_el()),
                Define("CONCURRENT_FACES", 
                    fplan.mb_aligned_floats*flux_par.p
                    //fplan.dofs_per_face()),
                Line(),
                ] + self.index_list_global_data().code + [
                Line(),
                flux_with_temp_data.struct,
                Line(),
                CudaShared(
                    ArrayOf(Value("index_list_entry_t", "smem_index_lists"),
                        "INDEX_LISTS_LENGTH")),
                CudaShared(Value("flux_data", "data")),
                CudaShared(ArrayOf(POD(float_type, "fluxes_on_faces"),
                    "MB_EL_COUNT*BLOCK_MB_COUNT*FACES_PER_EL*DOFS_PER_FACE"
                    )),
                Line(),
                ])

        S = Statement
        f_body = Block()
            
        f_body.extend(self.get_load_code(
            dest="smem_index_lists",
            base="gmem_index_lists",
            bytes="sizeof(index_list_entry_t)*INDEX_LISTS_LENGTH",
            descr="load index list data")
            )

        f_body.extend(self.get_load_code(
            dest="&data",
            base="gmem_data + blockIdx.x*DATA_BLOCK_SIZE",
            bytes="sizeof(flux_data)",
            descr="load face_pair data")
            +[ S("__syncthreads()"), Line() ])

        def flux_coeff_getter(flux_number_expr, prefix, flip_normal, internal_only):
            from hedge.cuda.cgen import make_multiple_ifs
            from pymbolic.mapper.stringifier import PREC_NONE
            if internal_only:
                int_coeff, ext_coeff = wdflux.fluxes[wdflux.interior_flux_number]
                return [
                        Initializer(
                            POD(float_type, "%sint_coeff" % prefix),
                            FluxToCodeMapper2(flip_normal)(int_coeff, PREC_NONE),
                            ),
                        Initializer(
                            POD(float_type, "%sext_coeff" % prefix),
                            FluxToCodeMapper2(flip_normal)(ext_coeff, PREC_NONE),
                            )
                        ]
            else:
                return [
                    POD(float_type, "%sint_coeff" % prefix),
                    POD(float_type, "%sext_coeff" % prefix),
                    make_multiple_ifs(
                        [
                        ("(%s) == %d" % (flux_number_expr, flux_nr),
                            Block([
                                Assign("%sint_coeff" % prefix, 
                                    FluxToCodeMapper2(flip_normal)(int_coeff, PREC_NONE),
                                    ),
                                Assign("%sext_coeff" % prefix, 
                                    FluxToCodeMapper2(flip_normal)(ext_coeff, PREC_NONE),
                                    ),
                                ])
                            )
                        for flux_nr, (int_coeff, ext_coeff)
                        in enumerate(wdflux.fluxes)
                        ],
                        base= Block([
                            Assign("%sint_coeff" % prefix, 0),
                            Assign("%sext_coeff" % prefix, 0),
                            ])
                        ),
                    ]

        def get_flux_code(is_bdry, is_twosided):
            flux_code = Block([])

            flux_code.extend([
                Initializer(Pointer(
                    Value("face_pair", "fpair")),
                    "data.facepairs+fpair_nr"),
                Initializer(Pointer(Value(
                    "index_list_entry_t", "a_ilist")),
                    "smem_index_lists + fpair->a_ilist_index"
                    ),
                Initializer(Pointer(Value(
                    "index_list_entry_t", "b_ilist")),
                    "smem_index_lists + fpair->b_ilist_index"
                    ),
                Initializer(
                    POD(float_type, "a_value"),
                    "tex1Dfetch(field_tex, fpair->a_base + a_ilist[facedof_nr])"
                    ),
                ])

            if is_bdry:
                flux_code.extend([
                    Initializer(
                        POD(float_type, "b_value"),
                        "tex1Dfetch(bfield_tex, fpair->b_base + b_ilist[facedof_nr])"
                        ),
                    ])
            else:
                flux_code.extend([
                    Initializer(
                        POD(float_type, "b_value"),
                        "tex1Dfetch(field_tex, fpair->b_base + b_ilist[facedof_nr])"
                        ),
                    ])

            flux_code.extend(
                    flux_coeff_getter("fpair->a_flux_number", "a_", 
                        flip_normal=False, internal_only=not is_bdry))

            if is_twosided:
                flux_code.extend(
                    flux_coeff_getter("fpair->b_flux_number_and_bdry_flag >> 1", 
                        "b_", flip_normal=True, internal_only=not is_bdry)
                    +[
                    Initializer(Pointer(Value(
                        "index_list_entry_t", "b_write_ilist")),
                        "smem_index_lists + fpair->b_write_ilist_index"
                        ),
                    ])

            flux_code.extend([
                Assign(
                    "fluxes_on_faces[fpair->a_dest+facedof_nr]",
                    "fpair->face_jacobian*("
                    "a_int_coeff*a_value+a_ext_coeff*b_value"
                    ")"),
                ])

            if is_twosided:
                flux_code.extend([
                    Assign(
                        "fluxes_on_faces[fpair->b_dest+b_write_ilist[facedof_nr]]",
                        "fpair->face_jacobian*("
                        "b_int_coeff*b_value+b_ext_coeff*a_value"
                        ")"
                        ),
                    ])

            flux_code.append(S("fpair_nr += CONCURRENT_FACES"))

            return flux_code

        f_body.extend_log_block("compute the fluxes", [Block([
            Initializer(Const(POD(numpy.int16, "block_face")),
                "THREAD_NUM / DOFS_PER_FACE"),
            Initializer(Const(POD(numpy.int16, "facedof_nr")),
                "THREAD_NUM - DOFS_PER_FACE*block_face"),
            If("facedof_nr < DOFS_PER_FACE && block_face < CONCURRENT_FACES",
                Block([
                    Initializer(POD(numpy.uint16, "fpair_nr"), "block_face"),
                    Comment("fluxes for dual-sided (intra-block) interior face pairs"),
                    While("fpair_nr < data.header.same_facepairs_end",
                        get_flux_code(is_bdry=False, is_twosided=True)
                        ),
                    Line(),
                    Comment("work around nvcc assertion failure"),
                    S("fpair_nr+=1"),
                    S("fpair_nr-=1"),
                    Line(),
                    Comment("fluxes for single-sided (inter-block) interior face pairs"),
                    While("fpair_nr < data.header.diff_facepairs_end",
                        get_flux_code(is_bdry=False, is_twosided=False)
                        ),
                    Line(),
                    Comment("fluxes for single-sided boundary face pairs"),
                    While("fpair_nr < data.header.bdry_facepairs_end",
                        get_flux_code(is_bdry=True, is_twosided=False)
                        ),
                ])
                )
            ]),
            S("__syncthreads()")
            ])

        f_body.extend_log_block("apply lifting matrix", [
            Initializer(Const(POD(numpy.uint16, "mb_el")),
                "MB_DOF/DOFS_PER_EL"),
            Initializer(Const(POD(numpy.uint16, "el_dof")),
                "MB_DOF - mb_el*DOFS_PER_EL"),
            For("unsigned base_mb = PAR_MB_NR",
                "base_mb < BLOCK_MB_COUNT",
                "base_mb += PAR_MB_COUNT", 
                Block([
                    Initializer(POD(float_type, "result"), 0),
                    #S("debugbuf[THREAD_NUM] = BASE_EL*FACES_PER_EL*DOFS_PER_FACE"),
                    #S("debugbuf[THREAD_NUM] = MB_DOF"),
                    ]+[
                        S("result += "
                            "tex2D(lift_matrix_tex, el_dof, %(facedof_nr)d)"
                            "*fluxes_on_faces[%(facedof_nr)d+BASE_EL*FACES_PER_EL*DOFS_PER_FACE]"
                            % {"facedof_nr":facedof_nr})
                        for facedof_nr in xrange(
                            fplan.faces_per_el()*fplan.dofs_per_face())
                    ]+[
                    Assign(
                        "flux[DOFS_BLOCK_BASE+base_mb*MB_DOF_COUNT+MB_DOF]",
                        "data.inverse_jacobians[BASE_EL]*result")
                    ])
                )
            ])

        # finish off ----------------------------------------------------------
        cmod.append(FunctionBody(f_decl, f_body))

        mod = cuda.SourceModule(cmod, 
                keep=True, 
                options=["--maxrregcount=12"]
                )
        print "lmem=%d smem=%d regs=%d" % (mod.lmem, mod.smem, mod.registers)

        liftmat_texref = mod.get_texref("lift_matrix_tex")
        if wdflux.is_lift:
            cuda.matrix_to_texref(fplan.ldis.lifting_matrix(), liftmat_texref)
        else:
            cuda.matrix_to_texref(fplan.ldis.multi_face_mass_matrix(), liftmat_texref)
        field_texref = mod.get_texref("field_tex")
        bfield_texref = mod.get_texref("bfield_tex")
        texrefs = [field_texref, bfield_texref, liftmat_texref]

        return mod.get_function("apply_flux"), texrefs, field_texref, bfield_texref

    # gpu data blocks ---------------------------------------------------------
    @memoize_method
    def gpu_diffmats(self, diff_op_cls, elgroup):

        discr = self.discr
        fplan = discr.flux_plan
        lplan = fplan.localop_plan()

        block_bytes = self.discr.devdata.align(
                fplan.dofs_per_el()
                *lplan.chunk_size
                *discr.dimensions
                *fplan.float_size)

        vstacked_matrices = [
                numpy.vstack(fplan.mb_elements*(m,))
                for m in diff_op_cls.matrices(elgroup)
                ]
                
        chunks = []

        for chunk_start in range(0, fplan.mb_elements*fplan.dofs_per_el(), lplan.chunk_size):
            diffmats = numpy.asarray(
                    numpy.hstack(
                        m[chunk_start:chunk_start+lplan.chunk_size] 
                        for m in vstacked_matrices
                        ),
                    dtype=self.discr.flux_plan.float_type,
                    order="C")
            chunks.append(buffer(diffmats))
        
        from pytools import Record
        from hedge.cuda.tools import pad_and_join
        return Record(
                device_memory=cuda.to_device(
                    pad_and_join(chunks, block_bytes)),
                block_bytes=block_bytes)

    @memoize_method
    def localop_rst_to_xyz(self, diff_op, elgroup):
        discr = self.discr
        d = discr.dimensions

        fplan = discr.flux_plan

        floats_per_block = d*d*fplan.elements_per_block()
        bytes_per_block = floats_per_block*fplan.float_size

        coeffs = diff_op.coefficients(elgroup)

        def get_el_index_in_el_group(el):
            mygroup, idx = discr.group_map[el.id]
            assert mygroup is elgroup
            return idx

        el_count = len(discr.blocks) * fplan.elements_per_block()
        elgroup_indices = numpy.zeros((el_count,), dtype=numpy.intp)
        for block in discr.blocks:
            block_elgroup_indices = [ get_el_index_in_el_group(el) 
                    for mb in block.microblocks 
                    for el in mb]
            offset = block.number * fplan.elements_per_block()
            elgroup_indices[offset:offset+len(block_elgroup_indices)] = \
                    block_elgroup_indices

        # indexed local, el_number, global
        result_matrix = (coeffs[:,:,elgroup_indices]
                .transpose(1,0,2))
        channels = discr.devdata.make_valid_tex_channel_count(d)
        add_channels = channels - result_matrix.shape[0]
        if add_channels:
            result_matrix = numpy.vstack((
                result_matrix,
                numpy.zeros((add_channels,d,el_count), dtype=result_matrix.dtype)
                ))

        assert result_matrix.shape == (channels, d, el_count)

        for block in discr.blocks:
            i = block.number * fplan.elements_per_block()
            for mb in block.microblocks:
                for el in mb:
                    egi = get_el_index_in_el_group(el)
                    assert egi == elgroup_indices[i]
                    assert (result_matrix[:d,:,i].T == coeffs[:,:,egi]).all()
                    i += 1

        return cuda.make_multichannel_2d_array(result_matrix)

    @memoize_method
    def flux_inverse_jacobians(self, elgroup):
        discr = self.discr
        d = discr.dimensions

        fplan = discr.flux_plan

        floats_per_block = fplan.elements_per_block()
        bytes_per_block = floats_per_block*fplan.float_size

        inv_jacs = elgroup.inverse_jacobians

        blocks = []
        
        def get_el_index_in_el_group(el):
            mygroup, idx = discr.group_map[el.id]
            assert mygroup is elgroup
            return idx

        from hedge.cuda.tools import pad
        for block in discr.blocks:
            block_elgroup_indices = numpy.fromiter(
                    (get_el_index_in_el_group(el) 
                        for mb in block.microblocks
                        for el in mb
                        ),
                    dtype=numpy.intp)

            block_inv_jacs = (inv_jacs[block_elgroup_indices].copy().astype(fplan.float_type))
            blocks.append(pad(str(buffer(block_inv_jacs)), bytes_per_block))
                
        from hedge.cuda.cgen import POD, ArrayOf
        return blocks, ArrayOf(
                POD(fplan.float_type, "inverse_jacobians"),
                floats_per_block)

    @memoize_method
    def flux_with_temp_data(self, wdflux, elgroup):
        discr = self.discr

        headers = []
        fp_blocks = []

        INVALID_DEST = (1<<16)-1

        from hedge.cuda.discretization import GPUBoundaryFaceStorage

        fp_struct = face_pair_struct(discr.flux_plan.float_type, discr.dimensions)

        outf = open("el_faces.txt", "w")
        for block in discr.blocks:
            ldis = block.local_discretization
            el_dofs = ldis.node_count()
            elface_dofs = ldis.face_node_count()*ldis.face_count()
            face_dofs = ldis.face_node_count()

            faces_todo = set((el,face_nbr)
                    for mb in block.microblocks
                    for el in mb
                    for face_nbr in range(ldis.face_count()))
            same_fp_structs = []
            diff_fp_structs = []
            bdry_fp_structs = []

            while faces_todo:
                elface = faces_todo.pop()

                a_face = discr.face_storage_map[elface]
                b_face = a_face.opposite

                print>>outf, "block %d el %d (global: %d) face %d" % (
                        block.number, discr.find_number_in_block(a_face.el_face[0]),
                        elface[0].id, elface[1]),
                        
                if isinstance(b_face, GPUBoundaryFaceStorage):
                    # boundary face
                    b_base = b_face.gpu_bdry_index_in_floats
                    a_flux_number = wdflux.boundary_elface_to_flux_number(
                            a_face.el_face)
                    b_flux_number = len(wdflux.fluxes) # invalid
                    b_load_from_bdry = 1
                    b_write_index_list = 0 # doesn't matter
                    b_dest = INVALID_DEST
                    print>>outf, "bdy%d" % a_flux_number

                    fp_structs = bdry_fp_structs
                else:
                    # interior face
                    b_base = discr.find_el_gpu_index(b_face.el_face[0])

                    a_flux_number = wdflux.interior_flux_number
                    b_flux_number = wdflux.interior_flux_number
                    b_load_from_bdry = 0

                    if b_face.native_block == a_face.native_block:
                        # same block
                        faces_todo.remove(b_face.el_face)
                        b_write_index_list = a_face.opp_write_index_list_id
                        b_dest = (
                                elface_dofs*discr.find_number_in_block(b_face.el_face[0])
                                +b_face.el_face[1]*face_dofs)

                        fp_structs = same_fp_structs

                        print>>outf, "same el %d (global: %d) face %d" % (
                                discr.find_number_in_block(b_face.el_face[0]), 
                                b_face.el_face[0].id, b_face.el_face[1])
                    else:
                        # different block
                        b_write_index_list = 0 # doesn't matter
                        b_dest = INVALID_DEST

                        fp_structs = diff_fp_structs

                        print>>outf, "diff"

                fp_structs.append(
                        fp_struct.make(
                            h=a_face.face_pair_side.h,
                            order=a_face.face_pair_side.order,
                            face_jacobian=a_face.face_pair_side.face_jacobian,
                            normal=a_face.face_pair_side.normal,

                            a_base=discr.find_el_gpu_index(a_face.el_face[0]),
                            b_base=b_base,

                            a_ilist_index= \
                                    a_face.global_int_flux_index_list_id*face_dofs,
                            b_ilist_index= \
                                    a_face.global_ext_flux_index_list_id*face_dofs,

                            a_flux_number=a_flux_number,
                            b_flux_number_and_bdry_flag=\
                                    (b_flux_number << 1) + b_load_from_bdry,
                            b_write_ilist_index= \
                                    b_write_index_list*face_dofs,

                            a_dest= \
                                    elface_dofs*discr.find_number_in_block(a_face.el_face[0])
                                    +a_face.el_face[1]*face_dofs,
                            b_dest=b_dest
                            ))

            headers.append(flux_header_struct().make(
                    els_in_block=len(block.el_number_map),
                    same_facepairs_end=\
                            len(same_fp_structs),
                    diff_facepairs_end=\
                            len(same_fp_structs)+len(diff_fp_structs),
                    bdry_facepairs_end=\
                            len(same_fp_structs)+len(diff_fp_structs)\
                            +len(bdry_fp_structs),
                    ))
            fp_blocks.append(same_fp_structs+diff_fp_structs+bdry_fp_structs)

        from hedge.cuda.cgen import Value
        from hedge.cuda.tools import make_superblocks

        return make_superblocks(
                discr.devdata, "flux_data",
                [
                    (headers, Value(flux_header_struct().tpname, "header")),
                    self.flux_inverse_jacobians(elgroup),
                    ],
                [ (fp_blocks, Value(fp_struct.tpname, "facepairs")), ])

    @memoize_method
    def index_list_global_data(self):
        discr = self.discr

        from pytools import single_valued
        ilist_length = single_valued(len(il) for il in discr.index_lists)

        if ilist_length > 256:
            tp = numpy.uint16
        else:
            tp = numpy.uint8

        from hedge.cuda.cgen import ArrayInitializer, ArrayOf, \
                Typedef, POD, Value, CudaConstant, Define

        from pytools import flatten, Record
        flat_ilists = numpy.array(
                list(flatten(discr.index_lists)),
                dtype=tp)
        return Record(
                code=[
                    Define("INDEX_LISTS_LENGTH", len(flat_ilists)),
                    Typedef(POD(tp, "index_list_entry_t")),
                    ],
                device_memory=cuda.to_device(flat_ilists)
                )