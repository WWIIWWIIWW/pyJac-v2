"""
kernel_gen.py - generators used for kernel creation
"""

import shutil
import textwrap
import os
import re
from string import Template
import logging
from collections import defaultdict
import six

import loopy as lp
from loopy.types import AtomicNumpyType, to_loopy_type
from loopy.version import LOOPY_USE_LANGUAGE_VERSION_2018_2  # noqa
from loopy.kernel.data import AddressSpace as scopes
try:
    import pyopencl as cl
except ImportError:
    cl = None
import numpy as np
import cgen

from pyjac.kernel_utils import file_writers as filew
from pyjac.kernel_utils.memory_manager import memory_manager, memory_limits, \
    memory_type, guarded_call
from pyjac import siteconf as site
from pyjac import utils
from pyjac.loopy_utils import loopy_utils as lp_utils
from pyjac.loopy_utils import preambles_and_manglers as lp_pregen
from pyjac.core.array_creator import problem_size as p_size
from pyjac.core.array_creator import work_size as w_size
from pyjac.core.array_creator import global_ind
from pyjac.core import array_creator as arc
from pyjac.core.enum_types import DriverType, KernelType
from pyjac.core import driver_kernels as drivers

script_dir = os.path.abspath(os.path.dirname(__file__))


class FakeCall(object):
    """
    In some cases, e.g. finite differnce jacobians, we need to place a dummy
    call in the kernel that loopy will accept as valid.  Then it needs to
    be substituted with an appropriate call to the kernel generator's kernel

    Attributes
    ----------
    dummy_call: str
        The dummy call passed to loopy to replaced during code-generation
    replace_in: :class:`loopy.LoopKernel` or :class:`kernel_generator`
        The kernel to replace the dummy call in.
        If :param:`replace_in` is :class:`kernel_generator`, then the
        :attr:`kernel_generator.kernel` will be used for replacement (i.e., the
        wrapping kernel)
    replace_with: :class:`loopy.LoopKernel` or :class:`kernel_generator`
        The kernel to replace the dummy call with.
        If :param:`replace_with` is :class:`kernel_generator`, then the
        :attr:`kernel_generator.kernel` will be used for replacement (i.e., the
        wrapping kernel)
    """

    def __init__(self, dummy_call, replace_in, replace_with):
        self.dummy_call = dummy_call
        self.replace_in = replace_in
        self._replace_with = replace_with

    def match(self, kernel, insns, for_driver):
        """
        Return true IFF :param:`kernel` matches :attr:`replace_in`
        """

        repl = self.replace_in
        if isinstance(self.replace_in, kernel_generator):
            repl = self.replace_in.kernel

        if for_driver:
            return kernel.name in insns and repl.name in insns
        else:
            return kernel.name in repl.name

    @property
    def replace_with(self):
        if isinstance(self._replace_with, kernel_generator):
            return self._replace_with.kernel
        return self._replace_with


class vecwith_fixer(object):

    """
    Simple utility class to force a constant vector width
    even when the loop being vectorized is shorted than the desired width

    clean : :class:`loopy.LoopyKernel`
        The 'clean' version of the kernel, that will be used for
        determination of the gridsize / vecsize
    vecsize : int
        The desired vector width
    """

    def __init__(self, clean, vecsize):
        self.clean = clean
        self.vecsize = vecsize

    def __call__(self, insn_ids, ignore_auto=False):
        # fix for variable too small for vectorization
        grid_size, lsize = self.clean.get_grid_sizes_for_insn_ids(
            insn_ids, ignore_auto=ignore_auto)
        lsize = lsize if not bool(self.vecsize) else \
            self.vecsize
        return grid_size, (lsize,)


def make_kernel_generator(loopy_opts, *args, **kwargs):
    """
    Factory generator method to return the appropriate
    :class:`kernel_generator` type based on the target language in the
    :param:`loopy_opts`

    Parameters
    ----------
    loopy_opts : :class:`LoopyOptions`
        The specified user options
    *args : tuple
        The other positional args to pass to the :class:`kernel_generator`
    **kwargs : dict
        The keyword args to pass to the :class:`kernel_generator`
    """
    if loopy_opts.lang == 'c':
        if not loopy_opts.auto_diff:
            return c_kernel_generator(loopy_opts, *args, **kwargs)
        if loopy_opts.auto_diff:
            return autodiff_kernel_generator(loopy_opts, *args, **kwargs)
    if loopy_opts.lang == 'opencl':
        return opencl_kernel_generator(loopy_opts, *args, **kwargs)
    if loopy_opts.lang == 'ispc':
        return ispc_kernel_generator(loopy_opts, *args, **kwargs)
    raise NotImplementedError()


def find_inputs_and_outputs(knl):
    """
    Convienence method that returns the name of all input/output array's for a given
    :class:`loopy.LoopKernel`

    Parameters
    ----------
    knl: :class:`loopy.LoopKernel`:
        The kernel to check

    Returns
    -------
    inputs_and_outputs: set of str
        The names of the written / read arrays
    """

    return (knl.get_read_variables() | knl.get_written_variables()) & \
        knl.global_var_names()


def _unSIMDable_arrays(knl, loopy_opts, mstore, warn=True):
    """
    Determined which  inputs / outputs are directly indexed with the base iname,
    or whether a map was applied.  In the latter case it is not safe to convert
    the array to a true vectorize access, as we have no guarentee that the
    index can be converted into an integer access

    Parameters
    ----------
    knl: :class:`loopy.LoopKernel`
        The loopy kernel to check
    loopy_opts: :class:`LoopyOptions`
        the loopy options object
    mstore: :class:`pyjac.core.array_creator.mapstore`
        The mapstore created for the kernel
    warn: bool [True]
        If true, fire off a warning of the arrays that could not be vectorized

    Returns
    -------
    unsimdable: list of str
        List of array names that cannot be safely converted to SIMD

    """

    if not loopy_opts.depth:
        # can convert all arrays to SIMD
        return []

    # this is test is made quite easy by checking the mapstore's tree

    # first, get all inputs / outputs
    io = find_inputs_and_outputs(knl)

    # check each array
    owners = arc.search_tree(mstore.absolute_root, io)
    cant_simd = []
    for ary, owner in zip(io, owners):
        # see if we can get from the owner to the absolute root without encountering
        # any non-affine transforms
        while owner and owner != mstore.absolute_root:
            if not owner.domain_transform.affine:
                cant_simd.append(ary)
                break
            owner = owner.parent

    if cant_simd and warn:
        logger = logging.getLogger(__name__)
        logger.warn('Arrays ({}) could not be fully vectorized. '
                    'You might achieve better performance by applying mechanism '
                    'sorting.'.format(utils.stringify_args(cant_simd)))
    return cant_simd


class kernel_generator(object):

    """
    The base class for the kernel generators
    """

    def __init__(self, loopy_opts, kernel_type, kernels,
                 namestore,
                 name=None,
                 external_kernels=[],
                 input_arrays=[],
                 output_arrays=[],
                 test_size=None,
                 auto_diff=False,
                 depends_on=[],
                 array_props={},
                 barriers=[],
                 extra_kernel_data=[],
                 extra_global_kernel_data=[],
                 extra_preambles=[],
                 is_validation=False,
                 fake_calls=[],
                 mem_limits='',
                 for_testing=False,
                 compiler=None,
                 driver_type=DriverType.lockstep,
                 generate_all=False):
        """
        Parameters
        ----------
        loopy_opts : :class:`LoopyOptions`
            The specified user options
        kernel_type : :class:`pyjac.enums.KernelType`
            The kernel type; used as a name and for inclusion of other headers
        kernels : list of :class:`loopy.LoopKernel`
            The kernels / calls to wrap
        namestore: :class:`NameStore`
            The namestore object used in creation of this kernel.
            This is used to pull any extra data (e.g. the Jacobian row/col inds)
            as needed
        input_arrays : list of str
            The names of the input arrays of this kernel
        output_arrays : list of str
            The names of the output arrays of this kernel
        test_size : int
            If specified, the # of conditions to test
        auto_diff : bool
            If true, this will be used for automatic differentiation
        depends_on : list of :class:`kernel_generator`
            If supplied, this kernel depends on the supplied depencies
        array_props : dict
            Mapping of various switches to array names:
                doesnt_need_init
                    * Arrays in this list do not need initialization
                      [defined for host arrays only]
        barriers : list of tuples
            List of global memory barriers needed, (knl1, knl2, barrier_type)
        extra_kernel_data : list of :class:`loopy.ArrayBase`
            Extra kernel arguements to add to this kernel
        extra_global_kernel_data : list of :class:`loopy.ArrayBase`
            Extra kernel arguements to add _only_ to this kernel (and not any
            subkernels)
        extra_preambles: list of :class:`PreambleGen`
            Preambles to add to subkernels
        is_validation: bool [False]
            If true, this kernel generator is being used to validate pyJac
            Hence we need to save our output data to a file
        fake_calls: list of :class:`FakeCall`
            Calls to smuggle past loopy
        mem_limits: str ['']
            Path to a .yaml file indicating desired memory limits that control the
            desired maximum amount of global / local / or constant memory that
            the generated pyjac code may allocate.  Useful for testing, or otherwise
            limiting memory usage during runtime. The keys of this file are the
            members of :class:`pyjac.kernel_utils.memory_manager.mem_type`
        for_testing: bool [False]
            If true, this kernel generator will be used for unit testing
        compiler: :class:`loopy.CCompiler` [None]
            An instance of a loopy compiler (or subclass there-of, e.g.
            :class:`pyjac.loopy_utils.AdeptCompiler`), or None
        driver_type: :class:`DriverType`
            The type of kernel driver to generate
        generate_all: bool [False]
            If true, generate driver / wrapper code for this kernel and _all_ it's
            dependencies, else simply generate driver / wrapper code for this kernel!
        """

        self.compiler = compiler
        self.loopy_opts = loopy_opts
        self.array_split = arc.array_splitter(loopy_opts)
        self.lang = loopy_opts.lang
        self.target = lp_utils.get_target(self.lang, self.loopy_opts.device,
                                          self.compiler)
        self.mem_limits = mem_limits

        # Used for pinned memory kernels to enable splitting evaluation over multiple
        # kernel calls
        self.arg_name_maps = {p_size: 'per_run'}

        self.mem = memory_manager(self.lang, self.loopy_opts.order,
                                  self.array_split,
                                  dev_type=self.loopy_opts.device_type)
        self.kernel_type = kernel_type
        self._name = name
        if name is not None:
            assert self.kernel_type == KernelType.dummy
        self.kernels = kernels
        self.namestore = namestore
        self.test_size = test_size
        self.auto_diff = auto_diff

        # update the memory manager
        self.mem.add_arrays(in_arrays=input_arrays, out_arrays=output_arrays)

        self.type_map = {}
        from loopy.types import to_loopy_type
        self.type_map[to_loopy_type(np.float64)] = 'double'
        self.type_map[to_loopy_type(np.int32)] = 'int'
        self.type_map[to_loopy_type(np.int64)] = 'long int'

        self.depends_on = depends_on[:]
        self.array_props = array_props.copy()
        self.all_arrays = []
        self.barriers = barriers[:]

        # extra kernel parameters to be added to subkernels
        self.extra_kernel_data = extra_kernel_data[:]
        # extra kernel parameters to be added only to this subkernel
        self.extra_global_kernel_data = extra_global_kernel_data[:]

        self.extra_preambles = extra_preambles[:]
        # check for Jacobian type
        if isinstance(namestore.jac, arc.jac_creator):
            # need to add the row / column inds
            self.extra_kernel_data.extend([self.namestore.jac_row_inds([''])[0],
                                           self.namestore.jac_col_inds([''])[0]])

            # and the preamble
            self.extra_preambles.append(lp_pregen.jac_indirect_lookup(
                self.namestore.jac_col_inds if self.loopy_opts.order == 'C'
                else self.namestore.jac_row_inds, self.target))

        # calls smuggled past loopy
        self.fake_calls = fake_calls.copy()
        # set testing
        self.for_testing = isinstance(test_size, int)
        # setup driver type
        self.driver_type = driver_type
        # whether to generate driver/wrappers for all dependencies
        self.generate_all = generate_all

        # mark owners
        self.owner = None

        def __mark(dep):
            for x in dep.depends_on:
                x.owner = dep
                __mark(x)
        __mark(self)

        # the base skeleton for sub kernel creation
        self.skeleton = textwrap.dedent(
            """
            for j
                ${pre}
                for ${var_name}
                    ${main}
                end
                ${post}
            end
            """)
        if not self.for_testing and self.loopy_opts.width:
            # fake split skeleton
            self.skeleton = textwrap.dedent(
                """
                for j_outer
                    for j_inner
                        ${pre}
                        for ${var_name}
                            ${main}
                        end
                        ${post}
                    end
                end
                """)

    @property
    def name(self):
        """
        Return the name of this kernel generator, based on :attr:`kernel_type
        """

        if self.kernel_type == KernelType.dummy:
            return self.name
        return utils.enum_to_string(self.kernel_type)

    @property
    def user_specified_work_size(self):
        """
        Return True IFF the user specified the :attr:`loopy_opts.work_size`
        """
        return self.loopy_opts.work_size is not None

    @property
    def work_size(self):
        """
        Returns either the integer :attr:`loopy_opts.work_size` (if specified by
        user) or the name of the `work_size` variable
        """

        if self.user_specified_work_size:
            return self.loopy_opts.work_size
        return w_size.name

    @property
    def target_preambles(self):
        """
        Preambles based on the target language

        Returns
        -------
        premables: list of str
            The string preambles for this :class:`kernel_generator`
        """

        return []

    @property
    def vec_width(self):
        """
        Returns the vector width of this :class:`kernel_generator`
        """
        if self.loopy_opts.depth:
            return self.loopy_opts.depth
        if self.loopy_opts.width:
            return self.loopy_opts.width
        return 0

    @property
    def hoist_locals(self):
        """
        If true (e.g., in a subclass), this type of generator requires that local
        memory be hoisted up to / defined in the type-level kernel.

        This is typically the case for languages such as OpenCL and CUDA, but not
        C / OpenMP
        """
        return False

    @property
    def file_prefix(self):
        """
        Prefix for filenames based on autodifferentiaton status
        """
        file_prefix = ''
        if self.auto_diff:
            file_prefix = 'ad_'
        return file_prefix

    def apply_barriers(self, instructions):
        """
        A method stud that can be overriden to apply synchonization barriers
        to vectorized code

        Parameters
        ----------

        instructions: list of str
            The instructions for this kernel

        Returns
        -------

        instructions : list of str
            The instructions passed in
        """
        return instructions

    def get_assumptions(self, test_size):
        """
        Returns a list of assumptions on the loop domains
        of generated subkernels

        Parameters
        ----------
        test_size : int or str
            In testing, this should be the integer size of the test data
            For production, this should the 'test_size' (or the corresponding)
            for the variable test size passed to the kernel

        Returns
        -------

        assumptions : list of str
            List of assumptions to apply to the generated sub kernel
        """

        if not self.for_testing:
            return []

        # set test size
        assumpt_list = ['{0} > 0'.format(p_size.name)]
        if bool(self.vec_width):
            assumpt_list.append('{0} mod {1} = 0'.format(
                p_size.name, self.vec_width))
        return assumpt_list

    def get_inames(self, test_size):
        """
        Returns the inames and iname_ranges for subkernels created using
        this generator

        Parameters
        ----------
        test_size : int or str
            In testing, this should be the integer size of the test data
            For production, this should the 'test_size' (or the corresponding)
            for the variable test size passed to the kernel

        Returns
        -------
        inames : list of str
            The string inames to add to created subkernels by default
        iname_domains : list of str
            The iname domains to add to created subkernels by default
        """

        # need to implement a fake split, to avoid loopy mangling the inner / outer
        # parallel inames
        fake_split = (not self.for_testing) and self.vec_width and \
            self.loopy_opts.width

        gind = global_ind
        if not self.for_testing:
            test_size = w_size.name

        if fake_split:
            gind += '_outer'

        inames = [gind]
        domains = ['0 <= {} < {}'.format(gind, test_size)]

        if fake_split:
            # add dummy j_inner domain
            inames += [global_ind + '_inner']
            domains += ['0 <= {} < {}'.format(inames[-1], self.vec_width)]

        return inames, domains

    def add_depencencies(self, k_gens):
        """
        Adds the supplied :class:`kernel_generator`s to this
        one's dependency list.  Functionally this means that this kernel
        generator will know how to compile and execute functions
        from the dependencies

        Parameters
        ----------
        k_gens : list of :class:`kernel_generator`
            The dependencies to add to this kernel
        """

        self.depends_on.extend(k_gens)

    def _make_kernels(self, kernels=[]):
        """
        Turns the supplied kernel infos into loopy kernels,
        and vectorizes them!

        Parameters
        ----------
        None

        Returns
        -------
        kernels: list of :class:`loopy.LoopKernel`
        """

        use_ours = False
        if not kernels:
            use_ours = True
            kernels = self.kernels

        # now create the kernels!
        for i, info in enumerate(kernels):
            # if external, or already built
            if isinstance(info, lp.LoopKernel):
                continue
            # create kernel from k_gen.knl_info
            kernels[i] = self.make_kernel(info, self.target, self.test_size)
            # apply vectorization
            kernels[i] = self.apply_specialization(
                self.loopy_opts,
                info.var_name,
                kernels[i],
                self.for_testing,
                vecspec=info.vectorization_specializer,
                can_vectorize=info.can_vectorize)

            cant_simd = []
            if self.loopy_opts.is_simd:
                # if SIMD we need to determine whether we can actually vectorize
                # the arrays in this kernel (sometimes we must leave them as)
                # unrolled vectors accesses
                cant_simd = _unSIMDable_arrays(kernels[i], self.loopy_opts,
                                               info.mapstore)

            # update the kernel args
            kernels[i] = self.array_split.split_loopy_arrays(kernels[i], cant_simd)

            # and add a mangler
            # func_manglers.append(create_function_mangler(kernels[i]))

            # set the editor
            kernels[i] = lp_utils.set_editor(kernels[i])

        # need to call make_kernels on dependencies
        for x in self.depends_on:
            if use_ours:
                x._make_kernels()

        return kernels

    def __copy_deps(self, scan_path, out_path, change_extension=True):
        """
        Convenience function to copy the dependencies of this
        :class:`kernel_generator` to our own output path

        Parameters
        ----------

        scan_path : str
            The path the dependencies were written to
        out_path : str
            The path this generator is writing to
        change_ext : bool
            If True, any dependencies that do not end with the proper file
            extension, see :any:`utils.file_ext`

        """
        deps = [x for x in os.listdir(scan_path) if os.path.isfile(
            os.path.join(scan_path, x)) and not x.endswith('.in')]
        for dep in deps:
            dep_dest = dep
            dep_is_header = dep.endswith('.h')
            ext = (utils.file_ext[self.lang] if not dep_is_header
                   else utils.header_ext[self.lang])
            if change_extension and not dep.endswith(ext):
                dep_dest = dep[:dep.rfind('.')] + ext
            shutil.copyfile(os.path.join(scan_path, dep),
                            os.path.join(out_path, dep_dest))

    def generate(self, path, data_order=None, data_filename='data.bin',
                 for_validation=False):
        """
        Generates wrapping kernel, compiling program (if necessary) and
        calling / executing program for this kernel

        Parameters
        ----------
        path : str
            The output path
        data_order : {'C', 'F'}f
            If specified, the ordering of the binary input data
            which may differ from the loopy order
        data_filename : Optional[str]
            If specified, the path to the data file for reading / execution
            via the command line
        for_validation: bool [False]
            If True, this kernel is being generated to validate pyJac, hence we need
            to save output data to a file

        Returns
        -------
        None
        """
        utils.create_dir(path)
        self._make_kernels()
        max_ic_per_run, max_ws_per_run, filename, kernel = \
            self._generate_wrapping_kernel(path)
        # set kernel object
        self.kernel = kernel
        self._generate_compiling_program(path, filename)
        self._generate_driver_kernel(path, kernel)
        self._generate_calling_program(path, data_filename, max_ic_per_run,
                                       max_ws_per_run, for_validation=for_validation)
        self._generate_calling_header(path)
        self._generate_common(path)

        # finally, copy any dependencies to the path
        lang_dir = os.path.join(script_dir, self.lang)
        self.__copy_deps(lang_dir, path, change_extension=False)

    def _generate_common(self, path):
        """
        Creates the common files (used by all target languages) for this
        kernel generator

        Parameters
        ----------
        path : str
            The output path for the common files

        Returns
        -------
        None
        """

        common_dir = os.path.join(script_dir, 'common')
        # get the initial condition reader
        with open(os.path.join(common_dir,
                               'read_initial_conditions.c.in'), 'r') as file:
            file_src = Template(file.read())

        with filew.get_file(os.path.join(path, 'read_initial_conditions'
                                         + utils.file_ext[self.lang]),
                            self.lang,
                            use_filter=False) as file:
            file.add_lines(file_src.safe_substitute(
                mechanism='mechanism' + utils.header_ext[self.lang],
                vectorization='vectorization' + utils.header_ext[self.lang]))

        # and any other deps
        self.__copy_deps(common_dir, path)

    def _get_pass(self, argv, include_type=True, is_host=True, postfix=''):
        """
        Simple helper method to get the string for passing an arguement
        to a method (or for the method definition)

        Parameters
        ----------
        argv : :class:`loopy.KernelArgument`
            The arguement to pass
        include_type : bool
            If True, include the C-type in the pass string [Default:True]
        postfix : str
            Optional postfix to append to the variable name [Default:'']
        """
        prefix = 'h_' if is_host else 'd_'
        return '{type}{prefix}{name}'.format(
            type=self.type_map[argv.dtype] + '* ' if include_type else '',
            prefix=prefix,
            name=argv.name + postfix)

    def _generate_calling_header(self, path):
        """
        Creates the header file for this kernel

        Parameters
        ----------
        path : str
            The output path for the header file

        Returns
        -------
        None
        """
        assert self.filename or self.bin_name, ('Cannot generate calling '
                                                'header before wrapping kernel'
                                                ' is generated...')
        with open(os.path.join(script_dir, self.lang,
                               'kernel.h.in'), 'r') as file:
            file_src = Template(file.read())

        self.header_name = os.path.join(path, self.file_prefix + self.name + '_main'
                                        + utils.header_ext[self.lang])
        with filew.get_file(os.path.join(self.header_name), self.lang,
                            use_filter=False) as file:
            file.add_lines(file_src.safe_substitute(
                input_args=', '.join([self._get_pass(next(
                    x for x in self.mem.arrays if x.name == a))
                    for a in self.mem.host_arrays
                    if not any(x.name == a for x in self.mem.host_constants)]),
                knl_name=self.name))

    def _special_kernel_subs(self, file_src):
        """
        Substitutes kernel template parameters that are specific to a
        target languages, to be specialized by subclasses of the
        :class:`kernel_generator`

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns
        -------
        new_file_src : str
            An updated kernel source string to substitute general template
            parameters into
        """
        return file_src

    def _special_wrapper_subs(self, file_src):
        """
        Substitutes wrapper kernel template parameters that are specific to a
        target languages, to be specialized by subclasses of the
        :class:`kernel_generator`

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns:
        new_file_src : Template
            An updated kernel source template to substitute general template
            parameters into
        """
        return file_src

    def _set_sort(self, arr):
        return sorted(set(arr), key=lambda x: arr.index(x))

    def _generate_calling_program(self, path, data_filename, max_ic_per_run,
                                  max_ws_per_run, for_validation=False):
        """
        Needed for all languages, this generates a simple C file that
        reads in data, sets up the kernel call, executes, etc.

        Parameters
        ----------
        path : str
            The output path to write files to
        data_filename : str
            The path to the data file for command line input
        max_ic_per_run: int
            The maximum # of initial conditions that can be evaluated per kernel
            call based on memory limits
        max_ws_per_run: int
            The maximum kernel work size
        for_validation: bool [False]
            If True, this kernel is being generated to validate pyJac, hence we need
            to save output data to a file

        Returns
        -------
        None
        """

        # find definitions
        mem_declares = self.mem.get_defns()

        # and input args

        # these are the args in the kernel defn
        knl_args = ', '.join([self._get_pass(
            next(x for x in self.mem.arrays if x.name == a))
            for a in self.mem.host_arrays
            if not any(x.name == a for x in self.mem.host_constants)])
        # these are the args passed to the kernel (exclude type)
        input_args = ', '.join([self._get_pass(
            next(x for x in self.mem.arrays if x.name == a),
            include_type=False) for a in self.mem.host_arrays
            if not any(x.name == a for x in self.mem.host_constants)])
        # these are passed from the main method (exclude type, add _local
        # postfix)
        local_input_args = ', '.join([self._get_pass(
            next(x for x in self.mem.arrays if x.name == a),
            include_type=False,
            postfix='_local') for a in self.mem.host_arrays
            if not any(x.name == a for x in self.mem.host_constants)])
        # create doc strings
        knl_args_doc = []
        knl_args_doc_template = Template(
            """
${name} : ${type}
    ${desc}
""")
        logger = logging.getLogger(__name__)
        for x in [y for y in self.mem.in_arrays if not any(
                z.name == y for z in self.mem.host_constants)]:
            if x == 'phi':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc='The state vector'))
            elif x == 'P_arr':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc='The array of pressures'))
            elif x == 'V_arr':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc='The array of volumes'))
            elif x == 'dphi':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc=('The time rate of change of'
                                                  'the state vector, in '
                                                  '{}-order').format(
                        self.loopy_opts.order)))
            elif x == 'jac':
                knl_args_doc.append(knl_args_doc_template.safe_substitute(
                    name=x, type='double*', desc=(
                        'The Jacobian of the time-rate of change of the state vector'
                        ' in {}-order').format(
                        self.loopy_opts.order)))
            else:
                logger.warn('Argument documentation not found for arg {}'.format(x))

        knl_args_doc = '\n'.join(knl_args_doc)
        # memory transfers in
        mem_in = self.mem.get_mem_transfers_in()
        # memory transfers out
        mem_out = self.mem.get_mem_transfers_out()
        # memory allocations
        mem_allocs = self.mem.get_mem_allocs()
        # input allocs
        local_allocs = self.mem.get_mem_allocs(True)
        # read args are those that aren't initalized elsewhere
        read_args = ', '.join(['h_' + x + '_local' for x in self.mem.in_arrays
                               if x in ['phi', 'P_arr', 'V_arr']])
        # memory frees
        mem_frees = self.mem.get_mem_frees()
        # input frees
        local_frees = self.mem.get_mem_frees(True)

        # get template
        with open(os.path.join(script_dir, self.lang,
                               'kernel.c.in'), 'r') as file:
            file_src = file.read()

        # specialize for language
        file_src = self._special_kernel_subs(file_src)

        # get data output
        if for_validation:
            num_outputs = len(self.mem.out_arrays)
            output_paths = ', '.join(['"{}"'.format(x + '.bin')
                                      for x in self.mem.out_arrays])
            outputs = ', '.join(['h_{}_local'.format(x)
                                 for x in self.mem.out_arrays])
            # get lp array map
            out_arrays = [next(x for x in self.mem.arrays if x.name == y)
                          for y in self.mem.out_arrays]
            output_sizes = ', '.join([str(self.mem._get_size(
                x, include_item_size=False)) for x in out_arrays])
        else:
            num_outputs = 0
            output_paths = ""
            outputs = ''
            output_sizes = ''

        with filew.get_file(os.path.join(path, self.name + '_main' + utils.file_ext[
                self.lang]), self.lang, use_filter=False) as file:
            file.add_lines(subs_at_indent(
                file_src,
                mem_declares=mem_declares,
                knl_args=knl_args,
                knl_args_doc=knl_args_doc,
                knl_name=self.name,
                input_args=input_args,
                local_input_args=local_input_args,
                mem_transfers_in=mem_in,
                mem_transfers_out=mem_out,
                mem_allocs=mem_allocs,
                mem_frees=mem_frees,
                read_args=read_args,
                order=self.loopy_opts.order,
                data_filename=data_filename,
                local_allocs=local_allocs,
                local_frees=local_frees,
                max_per_run=max_per_run,
                num_outputs=num_outputs,
                output_paths=output_paths,
                outputs=outputs,
                output_sizes=output_sizes
            ))

    def _generate_compiling_program(self, path, filename):
        """
        Needed for some languages (e.g., OpenCL) this may be overriden in
        subclasses to generate a program that compilers the kernel

        Parameters
        ----------
        path : str
            The output path for the compiling program
        filename : str
            The filename of the wrapping kernel

        Returns
        -------
        None
        """

        pass

    def __migrate_locals(self, kernel, ldecls):
        """
        Migrates local variables in :param:`ldecls` to the arguements of the
        given :param:`kernel`

        Parameters
        ----------
        kernel: :class:`loopy.LoopKernel`
            The kernel to modify
        ldecls: list of :class:`loopy.TemporaryVariable`
            The local variables to migrate

        Returns
        -------
        mod: :class:`loopy.LoopKernel`
            A modified kernel with the given local variables moved from the
            :attr:`loopy.LoopKernel.temporary_variables` to the kernel's
            :attr:`loopy.LoopKernel.args`

        """

        assert all(x.scope == scopes.LOCAL for x in ldecls)
        names = set([x.name for x in ldecls])
        from loopy.kernel.data import AddressSpace

        def __argify(temp):
            assert isinstance(temp, lp.TemporaryVariable)
            return lp.ArrayArg(
                **{k: v for k, v in six.iteritems(vars(temp))
                   if k in ['name', 'shape', 'dtype', 'dim_tags']},
                address_space=AddressSpace.LOCAL)
        return kernel.copy(
            args=kernel.args[:] + [__argify(x) for x in ldecls],
            temporary_variables={
                key: val for key, val in six.iteritems(kernel.temporary_variables)
                if not set([key]) & names})

    def __get_kernel_defn(self, knl, passed_locals=[]):
        """
        Returns the kernel definition string for this :class:`kernel_generator`,
        taking into account any migrated local variables

        Note: relies on building steps that occur in
        :func:`_generate_wrapping_kernel` -- will raise an error if called before
        this method

        Parameters
        ----------
        knl: None
            If supplied, this is used instead of the generated kernel
        passed_locals: list of :class:`cgen.CLLocal`
            __local variables declared in the wrapping kernel scope, that must
            be passed into this kernel, as __local defn's in subfunctions
            are not well defined, `function qualifiers in OpenCL <https://www.khronos.org/registry/OpenCL/sdk/1.2/docs/man/xhtml/functionQualifiers.html>` # noqa

        Returns
        -------
        defn: str
            The kernel definition
        """

        if knl is None:
            raise Exception('Must call _generate_wrapping_kernel first')

        remove_working = not self.user_specified_work_size

        if passed_locals:
            knl = self.__migrate_locals(knl, passed_locals)
        defn_str = lp_utils.get_header(knl)
        if remove_working:
            defn_str = self._remove_work_size(defn_str)
        return defn_str[:defn_str.index(';')]

    def _get_kernel_call(self, knl=None, passed_locals=[]):
        """
        Returns a function call for the given kernel :param:`knl` to be used
        as an instruction.

        If :param:`knl` is None, returns the kernel call for
        this :class:`kernel_generator`

        Parameters
        ----------
        knl: :class:`loopy.LoopKernel`
            The loopy kernel to generate a call for
        passed_locals: list of :class:`cgen.CLLocal`
            __local variables declared in the wrapping kernel scope, that must
            be passed into this kernel, as __local defn's in subfunctions
            are not well defined, `function qualifiers in OpenCL <https://www.khronos.org/registry/OpenCL/sdk/1.2/docs/man/xhtml/functionQualifiers.html>` # noqa

        Returns
        -------
        call: str
            The resulting function call
        """

        # default is the generated kernel
        if knl is None:
            args = self.kernel_data + [
                x for x in self.extra_global_kernel_data + self.extra_kernel_data
                if isinstance(x, lp.KernelArgument)]
            if passed_locals:
                # put a dummy object that we can reference the name of in the
                # arguements
                args += [type('', (object,), {'name': l.subdecl.name})
                         for l in passed_locals]
            name = self.name
        else:
            # otherwise used passed kernel
            if passed_locals:
                knl = self.__migrate_locals(knl, passed_locals)
            args = knl.args
            name = knl.name

        args = [x.name for x in args]

        return Template("${name}(${args});\n").substitute(
            name=name,
            args=', '.join(args)
            )

    def _compare_args(self, arg1, arg2):
        """
        Convenience method to test equality of :class:`loopy.KernelArgument`s

        Returns true IFF :param:`arg1` == :param:`arg2`, OR they differ only in
        their atomicity
        """

        def __atomify(arg):
            return arg.copy(dtype=to_loopy_type(arg.dtype, for_atomic=True,
                                                target=self.target))

        return arg1 == arg2 or (__atomify(arg1) == __atomify(arg2))

    def _process_args(self, kernels=[], use_subkernels=True):
        """
        Processes the arguements for all kernels in this generator (and subkernels
        from dependencies) to:

            1. Check for duplicates
            2. Separate arguments by type (Local / Global / Readonly / Value), etc.

        Notes
        -----
        First, note that the :class:`loopy.GlobalArg` is our own temporary
        work-around, and should be replaced after the upcoming kernel-call PR is
        merged into master.

        Second, the returned list of local arguments will be non-empty IFF the
        kernel generator's :attr:`hoist_locals`

        Parameters
        ----------
        kernels: list of :class:`loopy.LoopKernel`
            The kernels to process

        Returns
        -------
        global: list of :class:`loopy.GlobalArg`
            The list of global arguments for the top-level wrapping kernel
        local: list of :class:`loopy.TemporaryVariable` with :attr:`scope` LOCAL
            The list of local temporary variables to be defined at the top-level
            wrapping kernel.  These will be passed as :class:`loopy.LocalArg`s to
            subkernels
        readonly: set of str
            The names of arguements in the top-level kernel that are never written to
        constants: list of :class:`loopy.TemporaryVariable` with :attr:`scope` GLOBAL
                and :attr:`readonly` True
            The constant data to define in the top-level kernel
        valueargs: list of :class:`loopy.ValueArg`
            The value arguments passed into the top-level wrapping kernel
        """

        if not kernels:
            kernels = self.kernels[:]

        if use_subkernels:
            # add kernels we depend on
            kernels += [knl for dep in self.depends_on for knl in dep.kernels]

        # find complete list of kernel data
        args = [arg for dummy in kernels for arg in dummy.args]

        # add our additional kernel data, if any
        args.extend([x for x in self.extra_kernel_data if isinstance(
            x, lp.KernelArgument)])

        kernel_data = []
        # now, scan the arguments for duplicates
        nameset = sorted(set(d.name for d in args))
        for name in nameset:
            same_name = []
            for x in args:
                if x.name == name and not any(x == y for y in same_name):
                    same_name.append(x)
            if len(same_name) != 1:
                # need to see if differences are resolvable
                atomic = next((x for x in same_name if
                               isinstance(x.dtype, AtomicNumpyType)), None)

                def __raise():
                    raise Exception('Cannot resolve different arguements of '
                                    'same name: {}'.format(', '.join(
                                        str(x) for x in same_name)))

                if atomic is None or len(same_name) > 2:
                    # if we don't have an atomic, or we have multiple different
                    # args of the same name...
                    __raise()

                other = next(x for x in same_name if x != atomic)

                # check that all other properties are the same
                if not self._compare_args(other, atomic):
                    __raise()

                # otherwise, they're the same and the only difference is the
                # the atomic.

                # Hence, we try to copy all the other kernels with this arg in it
                # with the atomic arg
                for i, knl in enumerate(self.kernels):
                    if other in knl.args:
                        kernels[i] = knl.copy(args=[
                            x if x != other else atomic for x in knl.args])

                same_name.remove(other)

            same_name = same_name.pop()
            kernel_data.append(same_name)

        # split checked data into arguements and valueargs
        valueargs, args = utils.partition(
            kernel_data, lambda x: isinstance(x, lp.ValueArg))

        # get list of arguments on readonly
        readonly = set(
                arg.name for dummy in kernels for arg in dummy.args
                if not any(arg.name in d.get_written_variables()
                           for d in kernels)
                and not isinstance(arg, lp.ValueArg))

        # check (non-private) temporary variable duplicates
        temps = [arg for dummy in kernels
                 for arg in dummy.temporary_variables.values()
                 if isinstance(arg, lp.TemporaryVariable) and
                 arg.scope != scopes.PRIVATE and
                 arg.scope != lp.auto]
        # and add extra kernel data, if any
        temps.extend([x for x in self.extra_kernel_data if isinstance(
            x, lp.TemporaryVariable) and
            x.scope != scopes.PRIVATE and
            x.scope != lp.auto])
        copy = temps[:]
        temps = []
        for name in sorted(set(x.name for x in copy)):
            same_names = [x for x in copy if x.name == name]
            if len(same_names) > 1:
                if not all(x == same_names[0] for x in same_names[1:]):
                    raise Exception('Cannot resolve different arguements of '
                                    'same name: {}'.format(', '.join(
                                        str(x) for x in same_names)))
            temps.append(same_names[0])

        # work on temporary variables
        local = []
        if self.hoist_locals:
            # go through kernels finding local temporaries, and convert to local args
            for i, knl in enumerate(kernels):
                local_temps = [x for x in knl.temporary_variables.values()
                               if x.scope == scopes.LOCAL]
                if local_temps:
                    # convert kernel's local temporarys to local args
                    kernels[i] = self.__migrate_locals(knl, local_temps)
                    # and add to list
                    local.extend(local_temps)
                    # and remove from temps
                    temps = [x for x in temps if x not in local_temps]

        # finally, separate the constants from the temporaries
        # for opencl < 2.0, a constant global can only be a
        # __constant
        constants, temps = utils.partition(temps, lambda x: x.read_only)

        return args, local, readonly, constants, valueargs

    def _process_memory(self, args, readonly, local, constants):
        """
        Determine memory usage / limits, host constant migrations, etc.

        Parameters
        ----------
        args: list of :class:`loopy.KernelArgument`
            The kernel data (composed of :class:`loopy.GlobalArg`s and
            :class:`loopy.ValueArg`) to process
        readonly: set of str
            The kernel data that is never written to, may overlap with :param:`args`
        local: list of :class:`loopy.TemporaryVariable`
            The local temporaries to declare
        constants: list of :class:`loopy.TemporaryVariable`
            The constant data to declare in the kernel

        Returns
        -------
        updated_args: list of :class:`loopy.KernelArgument`
            The updated kernel arguments, possibly including any migrated host
            constants
        updated_constants: list of :class:`loopy.TemporaryVariable`
            The updated constant variables
        updated_readonly: list of str
            The updated readonly variables
        host_constants: list of :class:`loopy.GlobalArg`
            The __constant data that was necessary to move to __global data for
            space reasons
        mem_limits: :class:`memory_limits`
            The generated memory limit object
        """

        # now, do our memory calculations to determine if we can fit
        # all our data in memory
        mem_types = defaultdict(lambda: list())

        # store globals
        for arg in [x for x in args if not isinstance(x, lp.ValueArg)]:
            mem_types[memory_type.m_global].append(arg)

        # store locals
        mem_types[memory_type.m_local].extend(local)

        # and constants
        mem_types[memory_type.m_constant].extend(constants)

        # check if we're over our constant memory limit
        mem_limits = memory_limits.get_limits(
            self.loopy_opts, mem_types,
            string_strides=self.mem.string_strides,
            input_file=self.mem_limits,
            limit_int_overflow=self.loopy_opts.limit_int_overflow)

        host_constants = []
        if not all(mem_limits.can_fit()):
            # we need to convert our __constant temporary variables to
            # __global kernel args until we can fit
            type_changes = defaultdict(lambda: list())
            # we can't remove the sparse indicies as we can't pass pointers
            # to loopy preambles
            gtemps = [x for x in constants if 'sparse_jac' not in x.name]
            # sort by largest size
            gtemps = sorted(gtemps, key=lambda x: np.prod(x.shape), reverse=True)
            type_changes[memory_type.m_global].append(gtemps[0])
            gtemps = gtemps[1:]
            while not all(mem_limits.can_fit(with_type_changes=type_changes)):
                if not gtemps:
                    logger = logging.getLogger(__name__)
                    logger.exception('Cannot fit kernel {} in memory'.format(
                        self.name))
                    # should never get here, but still...
                    raise Exception()

                type_changes[memory_type.m_global].append(gtemps[0])
                gtemps = gtemps[1:]

            # once we've converted enough, we need to physically change the types
            for x in [v for arrs in type_changes.values() for v in arrs]:
                args.append(
                    lp.GlobalArg(x.name, dtype=x.dtype, shape=x.shape))
                readonly.add(readonly[-1].name)
                host_constants.append(x)

                # and update the types
                mem_types[memory_type.m_constant].remove(x)
                mem_types[memory_type.m_global].append(x)

            mem_limits = memory_limits.get_limits(
                self.loopy_opts, mem_types, string_strides=self.mem.string_strides,
                input_file=self.mem_limits,
                limit_int_overflow=self.loopy_opts.limit_int_overflow)

        return args, constants, readonly, host_constants, mem_limits

    def _dummy_wrapper_kernel(self, kernel_data, readonly, vec_width,
                              as_dummy_call=False, for_driver=False):
        """
        Generates a dummy loopy kernel to function as the global wrapper

        Parameters
        ----------
        kernel_data: list of :class:`loopy.
        vec_width: int [0]
            If non-zero, the vector width to use in kernel width fixing
        as_dummy_call: bool [False]
            If True, this is being generated as a dummy call smuggled past loopy
            e.g., for a Finite Difference jacobian call to the species rates kernel
            Hence, we need to add any :attr:`extra_kernel_data` to our kernel defn

        Returns
        -------
        knl: :class:`loopy.LoopKernel`
            The generated dummy kernel

        """

        # assign to non-readonly to prevent removal
        def _name_assign(arr, use_atomics=True):
            if arr.name not in readonly and not isinstance(arr, lp.ValueArg):
                return arr.name + '[{ind}] = 0 {atomic}'.format(
                    ind=', '.join(['0'] * len(arr.shape)),
                    atomic='{atomic}'
                           if isinstance(arr.dtype, AtomicNumpyType) and use_atomics
                           else '')
            return ''

        kdata = kernel_data[:]
        insns = '\n'.join(_name_assign(arr) for arr in kernel_data)
        name = self.name + ('_driver' if for_driver else '')

        if as_dummy_call:
            # add extra kernel args
            kdata.extend([x for x in self.extra_kernel_data
                          if isinstance(x, lp.KernelArgument)])

        # domains
        domains = ['{{[{iname}]: 0 <= {iname} < {size}}}'.format(
                iname='i',
                size=self.vec_width)]

        knl = lp.make_kernel(domains, insns, kdata, name=name,
                             target=self.target)

        if self.vec_width:
            ggs = vecwith_fixer(knl.copy(), self.vec_width)
            knl = knl.copy(overridden_get_grid_sizes_for_insn_ids=ggs)

        return knl

    def _migrate_host_constants(self, kernel, host_constants):
        """
        Moves temporary variables to global arguments based on the
        host constants for this :class:`kernel_generator`

        Parameters
        ----------
        kernel: :class:`loopy.LoopKernel`
            The kernel to transform
        host_constants: list of :class:`loopy.GlobalArg`
            The list of __constant temporary variables that were converted to
            __global args

        Returns
        -------
        migrated: :class:`loopy.LoopKernel`
            The kernel with any host constants transformed to input arguments
        """
        transferred = set([const.name for const in host_constants
                           if const.name in kernel.temporary_variables])
        # need to transfer these to arguments
        if transferred:
            # filter temporaries
            new_temps = {t: v for t, v in six.iteritems(
                         kernel.temporary_variables) if t not in transferred}
            # create new args
            new_args = [lp.GlobalArg(
                t, shape=v.shape, dtype=v.dtype, order=v.order,
                dim_tags=v.dim_tags)
                for t, v in six.iteritems(kernel.temporary_variables)
                if t in transferred]
            return kernel.copy(
                args=kernel.args + new_args, temporary_variables=new_temps)

        return kernel

    def _get_working_buffer(self, args):
        """
        Determine the size of the working buffer required to store the :param:`args`
        in a global working array, and return offsets for determing array indexing

        Parameters
        ----------
        args: list of :class:`loopy.KernelArguments`
            The kernel arguments to collapse into a working buffer

        Returns
        -------
        size_per_work_item: int
            The size (in number of doubles) of the working buffer per work-group
            item
        offsets: dict of str -> str
            A mapping of kernel argument names to string indicies to unpack working
            buffer into local pointers
        """

        size_per_work_item = 0
        offsets = {}
        work_size = self.work_size
        for arg in args:
            # ensure we're only operating on FP arrays
            assert not arg.dtype.is_integral()
            # split the shape into the work-item and other dimensions
            isizes, ssizes = utils.partition(arg.shape, lambda x: isinstance(x, int))
            # store offset and increment size
            offsets[arg.name] = '{} * {}'.format(size_per_work_item, work_size)
            size_per_work_item += int(np.prod(isizes))
            # check we have a work size in ssizes
            if not self.user_specified_work_size:
                assert len(ssizes) <= 1 and str(ssizes[0]) == w_size.name

        return size_per_work_item, offsets

    def _get_pointer_unpack(self, array, offset):
        """
        A method stub to implement the pattern:
        ```
            double* array = &rwk[offset]
        ```
        per target.  By default this returns the pointer unpack for C, but it may
        be overridden in subclasses

        Parameters
        ----------
        array: str
            The array name
        offset: str
            The stringified offset

        Returns
        -------
        unpack: str
            The stringified pointer unpacking statement
        """
        return 'double* {} = rwk + {};'.format(array, offset)

    def _remove_work_size(self, text):
        """
        Hack -- TODO: whip up define-based array sizing for loopy
        """

        replacers = [(re.compile(r'(int const work_size(?:, )?)'), ''),
                     (re.compile(r'(\(work_size, )'), '('),
                     (re.compile(r'(, work_size, )'), ', '),
                     (re.compile(r'(, work_size\))'), ')')]
        for r, s in replacers:
            text = r.sub(s, text)
        return text

    def _merge_kernels(self, for_driver, kernels=[], as_dummy_call=False,
                       fake_calls={}):
        """
        Generate and merge the supplied kernels, and return the resulting code in
        string form

        Parameters
        ----------
        for_driver: bool
            If True, these kernels are being merged for a kernel driver, and hence
            don't need to use a working-buffer
        kernels: list of :class:`loopy.LoopKernel`
            The kernels to merge, if not supplied, use :attr:`kernels`
        as_dummy_call: bool [False]
            If True, this is being generated as a dummy call smuggled past loopy
            e.g., for a Finite Difference jacobian call to the species rates kernel
            Hence, we need to add any :attr:`extra_kernel_data` to our kernel defn
        fake_calls: dict of str -> kernel_generator
            In some cases, e.g. finite differnce jacobians, we need to place a dummy
            call in the kernel that loopy will accept as valid.  Then it needs to
            be substituted with an appropriate call to the kernel generator's kernel

        Returns
        -------
        instructions: str
            The generated kernel instructions
        preambles: str
            The generated kernel preambles
        extra_kernels: list of str
            The generated kernels
        dummy_kernel: :class:`loopy.LoopKernel`
            The generated wrapper kernel
        mem_limits: :class:`memory_limits`
            The memory limit object for this kernel
        """

        if not kernels:
            kernels = self.kernels

        if not fake_calls:
            fake_calls = self.fake_calls

        # process arguments
        args, local, readonly, constants, valueargs = self._process_args(kernels)

        # process memory
        args, constants, readonly, host_constants, mem_limits = self._process_memory(
            args, readonly, local, constants)

        # update subkernels for host constants
        for i in range(len(kernels)):
            kernels[i] = self._migrate_host_constants(kernels[i], host_constants)

        # and add to memory manager
        self.mem.add_arrays(host_constants=host_constants)
        if not for_driver:
            self.mem.fix_arrays(args)

        # create the interior kernel
        # first, compress our kernel args into a working buffer
        offset_args = args[:]
        if for_driver:
            offset_args = [x for x in args if x.name.endswith('_local') and
                           x.shape[0] == self.work_size]
        size_per_wi, offsets = self._get_working_buffer(offset_args)
        # next, get the pointer unpackings
        # create working buffer
        working_buffer = lp.GlobalArg('rwk', shape=(w_size.name, size_per_wi),
                                      order=self.loopy_opts.order,
                                      dtype=np.float64)
        kernel_data = [w_size] + [working_buffer]

        # and add to the memory store
        if not for_driver:
            self.mem.add_arrays([working_buffer])
        else:
            # add the working buffer to the driver function
            for i, kernel in enumerate(kernels):
                if kernel.name.endswith('driver'):
                    kargs = kernel.args[:]
                    for arg in kernel_data:
                        if arg not in kargs:
                            kargs.append(arg)
                    kernels[i] = kernel.copy(args=kargs)
                    break

        # and finally, generate the kernel code
        preambles = []
        extra_kernels = []
        inits = []
        instructions = []
        local_decls = []

        def _get_func_body(cgr, subs={}):
            """
            Returns the function declaration w/o initializers or preambles
            from a :class:`loopy.GeneratedProgram`
            """
            # get body
            if isinstance(cgr.ast, cgen.FunctionBody):
                body = str(cgr.ast)
            else:
                body = str(cgr.ast.contents[-1])

            # apply any substitutions
            for k, v in six.iteritems(subs):
                body = body.replace(k, v)

            # feed through get_code to get any corrections
            return lp_utils.get_code(body, self.loopy_opts)

        # stubs for kernel calls snuck past loopy
        extra_fake_kernels = []

        # split into bodies, preambles, etc.
        for i, k, in enumerate(kernels):
            # make kernel
            cgr = lp.generate_code_v2(k)
            # grab preambles
            for _, preamble in cgr.device_preambles:
                if preamble not in preambles:
                    preambles.append(preamble)

            # now scan device program
            assert len(cgr.device_programs) == 1
            cgr = cgr.device_programs[0]
            init_list = []
            if isinstance(cgr.ast, cgen.Collection):
                # look for preambles
                for item in cgr.ast.contents:
                    # initializers go in the preamble
                    if isinstance(item, cgen.Initializer):
                        def _rec_check_name(decl):
                            if 'name' in vars(decl):
                                return decl.name in readonly
                            elif 'subdecl' in vars(decl):
                                return _rec_check_name(decl.subdecl)
                            return False
                        # check for migrated constant
                        if _rec_check_name(item.vdecl):
                            continue
                        if str(item) not in inits:
                            init_list.append(str(item))

                    # blanklines and bodies can be ignored (as they will be added
                    # below)
                    elif not (isinstance(item, cgen.Line)
                              or isinstance(item, cgen.FunctionBody)):
                        raise NotImplementedError(type(item))
            else:
                # no preambles / initializers
                assert isinstance(cgr.ast, cgen.FunctionBody)

            # and add to inits
            inits.extend(init_list)

            # we need to place the call in the instructions and the extra kernels
            # in their own array
            extra_kernels.append(self._remove_work_size(_get_func_body(cgr, {})))
            insns = self._remove_work_size(self._get_kernel_call(k))

            if fake_calls:
                # check to see if this kernel has a fake call to replace
                fk = next((x for x in fake_calls if x.match(k, insns, for_driver)),
                          None)
                if fk:
                    # mark for replacement
                    extra_fake_kernels.append((fk, len(extra_kernels) - 1))

            instructions.append(insns)

        # fix extra fake kernel calls
        for (fake_call, index) in extra_fake_kernels:
            # replace call in instructions to call to kernel
            knl_call = self._remove_work_size(self._get_kernel_call(
                knl=fake_call.replace_with, passed_locals=local_decls))
            extra_kernels[index] = extra_kernels[index].replace(
                fake_call.dummy_call, knl_call[:-2])

        # determine vector width
        vec_width = self.loopy_opts.depth
        if not bool(vec_width):
            vec_width = self.loopy_opts.width
        if not bool(self.vec_width):
            vec_width = 0

        # and save kernel data
        kernel = self._dummy_wrapper_kernel(
            kernel_data, readonly, vec_width, as_dummy_call=as_dummy_call,
            for_driver=for_driver)
        # and split
        kernel = self.array_split.split_loopy_arrays(kernel)

        # get working buffer indexing, if necessary
        pointer_unpacks = []
        for k, v in six.iteritems(offsets):
            pointer_unpacks.append(self._get_pointer_unpack(k, v))

        # insert barriers if any
        if not for_driver:
            instructions = self.apply_barriers(instructions)

        # add pointer unpacking
        instructions[0:0] = pointer_unpacks[:]

        # add local declaration to beginning of instructions
        instructions[0:0] = [str(x) for x in local_decls]

        # join to str
        instructions = '\n'.join(instructions)
        # add any target preambles
        preambles = preambles + [x for x in self.target_preambles
                                 if x not in preambles]
        preambles = '\n'.join(textwrap.dedent(x) for x in preambles + inits)
        # join to string
        extra_kernels = '\n'.join(extra_kernels)

        return instructions, preambles, extra_kernels, kernel, mem_limits

    def _generate_wrapping_kernel(self, path, instruction_store=None,
                                  as_dummy_call=False):
        """
        Generates a wrapper around the various subkernels in this
        :class:`kernel_generator` (rather than working through loopy's fusion)

        Parameters
        ----------
        path : str
            The output path to write files to
        instruction_store: dict [None]
            If supplied, store the generated instructions for this kernel
            in this store to avoid duplicate work
        as_dummy_call: bool [False]
            If True, this is being generated as a dummy call smuggled past loopy
            e.g., for a Finite Difference jacobian call to the species rates kernel
            Hence, we need to add any :attr:`extra_kernel_data` to our kernel defn

        Returns
        -------
        max_per_run: int
            The maximum number of initial conditions that can be executed per
            kernel call
        """

        assert all(
            isinstance(x, lp.LoopKernel) for x in self.kernels), (
            'Cannot generate wrapper before calling _make_kernels')

        if self.depends_on and self.generate_all:
            # generate wrappers for dependencies
            raise NotImplementedError

        # get the instructions, preambles and kernel
        instructions, preambles, extra_kernels, kernel, mem_limits \
            = self._merge_kernels(False, self.kernels)

        filename = self._to_file(
            path, instructions, preambles, kernel, extra_kernels)

        max_ic_per_run, max_ws_per_run = mem_limits.can_fit(memory_type.m_global)
        # normalize to divide evenly into vec_width
        if self.vec_width != 0:
            max_ic_per_run = np.floor(
                max_ic_per_run / self.vec_width) * self.vec_width

        return int(max_ic_per_run), int(max_ws_per_run), filename, kernel

    def _to_file(self, path, instructions, preambles, kernel, extra_kernels,
                 for_driver=False):
        """
        Write the generated kernel data to file

        Parameters
        ----------
        path: str
            The directory to write to
        instructions: str
            The interior kernel instructions for :param:`kernel
        preambles: str
            The preambles
        kernel: :class:`loopy.LoopKernel`
            The kernel definition to write to file
        extra_kernels: str
            Kernels called by this
        for_driver: bool [False]
            Whether we're writing a driver kernel or not

        Returns
        -------
        filename: str
            The name of the generated file
        """

        # get filename
        basename = self.name
        name = basename
        if for_driver:
            name += '_driver'

        # first, load the wrapper as a template
        with open(os.path.join(
                script_dir,
                self.lang,
                'wrapping_kernel{}.in'.format(utils.file_ext[self.lang])),
                'r') as file:
            file_str = file.read()
            file_src = Template(file_str)

        file_src = self._special_wrapper_subs(file_src)

        # create the file
        filename = os.path.join(path, self.file_prefix + name + utils.file_ext[
            self.lang])
        with filew.get_file(filename, self.lang, include_own_header=True) as file:
            instructions = _find_indent(file_str, 'body', instructions)
            preambles = _find_indent(file_str, 'preamble', preambles)
            lines = file_src.safe_substitute(
                defines='',
                preamble='',
                func_define=self.__get_kernel_defn(kernel),
                body=instructions,
                extra_kernels=extra_kernels)

            if self.auto_diff:
                lines = [x.replace('double', 'adouble') for x in lines]
            file.add_lines(lines)

        # and the header file
        headers = []
        if for_driver:
            # include header to base call
            headers.append(basename + utils.header_ext[self.lang])
        else:
            # include sub kernels
            for x in self.depends_on:
                headers.append(x.name + utils.header_ext[self.lang])

        # include the preambles as well, such that they can be
        # included into other files to avoid duplications
        preambles = preambles.split('\n')
        preambles.extend([
            self.__get_kernel_defn(kernel) + utils.line_end[self.lang]])

        with filew.get_header_file(
            os.path.join(path, self.file_prefix + name + utils.header_ext[
                self.lang]), self.lang) as file:

            file.add_headers(headers)
            lines = '\n'.join(preambles).split('\n')
            if self.auto_diff:
                file.add_headers('adept.h')
                file.add_lines('using adept::adouble;\n')
                lines = [x.replace('double', 'adouble') for x in lines]
            file.add_lines(lines)

        return filename

    def _generate_driver_kernel(self, path, driven):
        """
        Generates a driver kernel that is responsible for looping through the entire
        set of initial conditions for testing / execution.  This is useful so that
        an external program can easily link to the wrapper kernel generated by this
        :class:`kernel_generator` and handle their own iteration over conditions
        (e.g., as in an ODE solver). :see:`driver-function` for more

        Parameters
        ----------
        path: str
            The path to place the driver kernel in
        driven: :class:`loopy.LoopKernel
            The kernel to drive!
        Returns
        -------
        None
        """

        knl_info = drivers.get_driver(
                self.loopy_opts, self.namestore, self.mem.in_arrays,
                self.mem.out_arrays, self, test_size=self.test_size)

        if self.driver_type == DriverType.lockstep:
            template = drivers.lockstep_driver_template(self.loopy_opts, self)
        else:
            raise NotImplementedError

        # make driver kernel
        kernels = self._make_kernels(knl_info)
        instructions, preambles, extra_kernels, kernel, mem_limits = \
            self._merge_kernels(True, kernels, fake_calls=[FakeCall(
                self.name + '()', kernels[1], self)])
        instructions = subs_at_indent(template, insns=instructions)
        # and write to file
        self._to_file(path, instructions, preambles, kernel, extra_kernels,
                      for_driver=True)

    def remove_unused_temporaries(self, knl):
        """
        Convenience method to remove unused temporary variables from created
        :class:`loopy.LoopKernel`'s

        ...with exception of the arrays used in the preambles
        """
        new_args = []

        exp_knl = lp.expand_subst(knl)

        refd_vars = set(knl.all_params())
        for insn in exp_knl.instructions:
            refd_vars.update(insn.dependency_names())

        from loopy.kernel.array import ArrayBase, FixedStrideArrayDimTag
        from loopy.symbolic import get_dependencies
        from itertools import chain

        def tolerant_get_deps(expr, parse=False):
            if expr is None or expr is lp.auto:
                return set()
            if parse and isinstance(expr, tuple):
                from loopy.kernel.array import _pymbolic_parse_if_necessary
                expr = tuple(_pymbolic_parse_if_necessary(x) for x in expr)
            return get_dependencies(expr)

        for ary in chain(knl.args, six.itervalues(knl.temporary_variables)):
            if isinstance(ary, ArrayBase):
                refd_vars.update(
                    tolerant_get_deps(ary.shape)
                    | tolerant_get_deps(ary.offset, parse=True))

                for dim_tag in ary.dim_tags:
                    if isinstance(dim_tag, FixedStrideArrayDimTag):
                        refd_vars.update(
                            tolerant_get_deps(dim_tag.stride))

        for arg in knl.temporary_variables:
            if arg in refd_vars:
                new_args.append(arg)

        return knl.copy(temporary_variables={arg: knl.temporary_variables[arg]
                                             for arg in new_args})

    def make_kernel(self, info, target, test_size):
        """
        Convience method to create loopy kernels from :class:`knl_info`'s

        Parameters
        ----------
        info : :class:`knl_info`
            The rate contstant info to generate the kernel from
        target : :class:`loopy.TargetBase`
            The target to generate code for
        test_size : int/str
            The integer (or symbolic) problem size

        Returns
        -------
        knl : :class:`loopy.LoopKernel`
            The generated loopy kernel
        """

        # and the skeleton kernel
        skeleton = self.skeleton[:]

        # convert instructions into a list for convienence
        instructions = info.instructions
        if isinstance(instructions, str):
            instructions = textwrap.dedent(info.instructions)
            instructions = [x for x in instructions.split('\n') if x.strip()]

        # load inames
        if not info.iname_domain_override:
            our_inames, our_iname_domains = self.get_inames(test_size)
        else:
            our_inames, our_iname_domains = zip(*info.iname_domain_override)
            our_inames, our_iname_domains = list(our_inames), \
                list(our_iname_domains)

        inames = [info.var_name] + our_inames
        # add map instructions
        instructions = list(info.mapstore.transform_insns) + instructions

        # look for extra inames, ranges
        iname_range = []

        assumptions = info.assumptions[:]

        # find the start index for 'i'
        iname, iname_domain = info.mapstore.get_iname_domain()

        # add to ranges
        iname_range.append(iname_domain)
        iname_range.extend(our_iname_domains)

        assumptions = []
        assumptions.extend(self.get_assumptions(test_size))

        for iname, irange in info.extra_inames:
            inames.append(iname)
            iname_range.append(irange)

        # construct the kernel args
        pre_instructions = info.pre_instructions[:]
        post_instructions = info.post_instructions[:]

        def subs_preprocess(key, value):
            # find the instance of ${key} in kernel_str
            result = _find_indent(skeleton, key, value)
            return Template(result).safe_substitute(var_name=info.var_name)

        kernel_str = Template(skeleton).safe_substitute(
            var_name=info.var_name,
            pre=subs_preprocess('${pre}', '\n'.join(pre_instructions)),
            post=subs_preprocess('${post}', '\n'.join(post_instructions)),
            main=subs_preprocess('${main}', '\n'.join(instructions)))

        # finally do extra subs
        if info.extra_subs:
            kernel_str = Template(kernel_str).safe_substitute(
                **info.extra_subs)

        iname_arr = []
        # generate iname strings
        for iname, irange in zip(*(inames, iname_range)):
            iname_arr.append(Template(
                '{[${iname}]:${irange}}').safe_substitute(
                iname=iname,
                irange=irange
            ))

        # get extra mapping data
        extra_kernel_data = [domain(node.iname)[0] for domain, node in
                             six.iteritems(info.mapstore.domain_to_nodes)
                             if not node.is_leaf()]

        extra_kernel_data += self.extra_kernel_data[:]

        # check for duplicate kernel data (e.g. multiple phi arguements)
        kernel_data = []
        for k in info.kernel_data + extra_kernel_data:
            if k not in kernel_data:
                kernel_data.append(k)

        # make the kernel
        knl = lp.make_kernel(iname_arr,
                             kernel_str,
                             kernel_data=kernel_data,
                             name=info.name,
                             target=target,
                             assumptions=' and '.join(assumptions),
                             default_offset=0,
                             **info.kwargs
                             )
        # fix parameters
        if info.parameters:
            knl = lp.fix_parameters(knl, **info.parameters)
        if self.user_specified_work_size:
            # fix work size
            knl = lp.fix_parameters(knl, **{w_size.name: self.work_size})
        # prioritize and return
        knl = lp.prioritize_loops(knl, [y for x in inames
                                        for y in x.split(',')])
        # check manglers
        if info.manglers:
            knl = lp.register_function_manglers(knl, info.manglers)

        preambles = info.preambles + self.extra_preambles[:]
        # check preambles
        if preambles:
            # register custom preamble functions
            knl = lp.register_preamble_generators(knl, preambles)
            # also register their function manglers
            knl = lp.register_function_manglers(knl, [
                p.func_mangler for p in preambles])

        return self.remove_unused_temporaries(knl)

    @classmethod
    def apply_specialization(cls, loopy_opts, inner_ind, knl, for_testing,
                             vecspec=None, can_vectorize=True,
                             get_specialization=False):
        """
        Applies wide / deep vectorization and/or ILP loop unrolling
        to a loopy kernel

        Parameters
        ----------
        loopy_opts : :class:`loopy_options` object
            A object containing all the loopy options to execute
        inner_ind : str
            The inner loop index variable
        knl : :class:`loopy.LoopKernel`
            The kernel to transform
        for_testing: bool [False]
            If False, apply fake split for wide-vectorizations
        vecspec : :function:
            An optional specialization function that is applied after
            vectorization to fix hanging loopy issues
        can_vectorize : bool
            If False, cannot be vectorized in the normal manner, hence
            vecspec must be used to vectorize.
        get_specialization : bool [False]
            If True, the specialization will not be _applied_ to the kernel, instead
            a dictionary mapping inames -> tags will be returned

        Returns
        -------
        knl : :class:`loopy.LoopKernel`
            The transformed kernel

        OR

        iname_map: dict
            A dictionary mapping inames -> tags, only returned if
            :param:`get_specialization` is True
        """

        # before doing anything, find vec width
        # and split variable
        vec_width = None
        to_split = None
        i_tag = inner_ind
        j_tag = global_ind
        depth = loopy_opts.depth
        width = loopy_opts.width
        if depth:
            to_split = inner_ind
            vec_width = depth
            i_tag += '_outer'
        elif width:
            to_split = global_ind
            vec_width = width
            j_tag += '_outer'
        if not can_vectorize:
            assert vecspec is not None, ('Cannot vectorize a non-vectorizable '
                                         'kernel {} without a specialized '
                                         'vectorization function'.format(
                                             knl.name))
        specialization = {}

        # if we're splitting
        # apply specified optimizations
        if to_split and can_vectorize:
            # and assign the l0 axis to the correct variable
            tag = 'vec' if loopy_opts.is_simd else 'l.0'
            if get_specialization:
                specialization[to_split + '_inner'] = tag
            elif loopy_opts.width and not for_testing:
                # apply the fake split
                knl = lp.tag_inames(knl, [(to_split + '_inner', tag)])
            else:
                knl = lp.split_iname(knl, to_split, vec_width, inner_tag=tag)

        if utils.can_vectorize_lang[loopy_opts.lang]:
            # tag 'global_ind' as g0, use simple parallelism
            if get_specialization:
                specialization[j_tag] = 'g.0'
            else:
                knl = lp.tag_inames(knl, [(j_tag, 'g.0')])

        # if we have a specialization
        if vecspec and not get_specialization:
            knl = vecspec(knl)

        if bool(vec_width) and not loopy_opts.is_simd and not get_specialization:
            # finally apply the vector width fix above
            ggs = vecwith_fixer(knl.copy(), vec_width)
            knl = knl.copy(overridden_get_grid_sizes_for_insn_ids=ggs)

        # now do unr / ilp
        if loopy_opts.unr is not None:
            if get_specialization:
                specialization[i_tag + '_inner'] = 'unr'
            else:
                knl = lp.split_iname(knl, i_tag, loopy_opts.unr, inner_tag='unr')
        elif loopy_opts.ilp:
            if get_specialization:
                specialization[i_tag] = 'ilp'
            else:
                knl = lp.tag_inames(knl, [(i_tag, 'ilp')])

        return knl if not get_specialization else specialization


class c_kernel_generator(kernel_generator):

    """
    A C-kernel generator that handles OpenMP parallelization
    """

    def __init__(self, *args, **kwargs):

        super(c_kernel_generator, self).__init__(*args, **kwargs)

        self.extern_defn_template = Template(
            'extern ${type}* ${name}' + utils.line_end[self.lang])

    @property
    def target_preambles(self):
        """
        Preambles for OpenMP

        Notes
        -----
        This defines the work-size variable for OpenCL as the number of groups
        launched by the OpenCL kernel (if the user has not specified a value)

        Returns
        -------
        premables: list of str
            The string preambles for this :class:`kernel_generator`
        """

        if self.user_specified_work_size:
            return []

        work_size = """
        #ifndef work_size
            #define work_size (omp_get_num_threads())
        #endif
        """

        return [work_size]

    def get_assumptions(self, test_size):
        """
        Returns a list of assumptions on the loop domains
        of generated subkernels

        For the C-kernels, the problem_size is abstracted out into the wrapper
        kernel's OpenMP loop.

        Additionally, there is no concept of a "vector width", hence
        we return an empty assumption set

        Parameters
        ----------
        test_size : int or str
            In testing, this should be the integer size of the test data
            For production, this should the 'test_size' (or the corresponding)
            for the variable test size passed to the kernel

        Returns
        -------

        assumptions : list of str
            List of assumptions to apply to the generated sub kernel
        """

        return []

    def _special_kernel_subs(self, file_src):
        """
        An override of the :method:`kernel_generator._special_wrapping_subs`
        that implements C-specific wrapping kernel arguement passing

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns
        -------
        new_file_src : str
            An updated kernel source string to substitute general template
            parameters into
        """

        # and input args

        # these are the args in the kernel defn
        full_kernel_args = ', '.join(self._set_sort(
            [self._get_pass(a, include_type=False, is_host=False)
             for a in self.mem.arrays]))

        return Template(file_src).safe_substitute(
            full_kernel_args=full_kernel_args)


class autodiff_kernel_generator(c_kernel_generator):

    """
    A C-Kernel generator specifically designed to work with the
    autodifferentiation scheme.  Handles adding jacobian, etc.
    """

    def __init__(self, *args, **kwargs):

        from pyjac.loopy_utils.loopy_utils import AdeptCompiler
        kwargs.setdefault('compiler', AdeptCompiler())
        super(autodiff_kernel_generator, self).__init__(*args, **kwargs)

    def add_jacobian(self, jacobian):
        """
        Adds the jacobian object to the extra kernel data for inclusion in
        generation (to be utilized during the edit / AD process)

        Parameters
        ----------

        jacobian : :class:`loopy.GlobalArg`
            The loopy arguement to add to the method signature

        Returns
        -------
        None
        """

        self.extra_kernel_data.append(jacobian)


class ispc_kernel_generator(kernel_generator):

    def __init__(self, *args, **kwargs):
        super(ispc_kernel_generator, self).__init__(*args, **kwargs)

    # TODO: fill in


class opencl_kernel_generator(kernel_generator):

    """
    An opencl specific kernel generator
    """

    def __init__(self, *args, **kwargs):
        super(opencl_kernel_generator, self).__init__(*args, **kwargs)

        # opencl specific items
        self.set_knl_arg_array_template = Template(
            guarded_call(self.lang, 'clSetKernelArg(kernel, ${arg_index}, '
                         '${arg_size}, ${arg_value})'))
        self.set_knl_arg_value_template = Template(
            guarded_call(self.lang, 'clSetKernelArg(kernel, ${arg_index}, '
                         '${arg_size}, ${arg_value})'))
        self.barrier_templates = {
            'global': 'barrier(CLK_GLOBAL_MEM_FENCE)',
            'local': 'barrier(CLK_LOCAL_MEM_FENCE)'
        }

        # add atomic types to typemap
        from loopy.types import to_loopy_type
        # these don't need to be volatile, as they are on the host side
        self.type_map[to_loopy_type(np.float64, for_atomic=True)] = 'double'
        self.type_map[to_loopy_type(np.int32, for_atomic=True)] = 'int'
        self.type_map[to_loopy_type(np.int64, for_atomic=True)] = 'long int'

    @property
    def target_preambles(self):
        """
        Preambles for OpenCL

        Notes
        -----
        This defines the work-size variable for OpenCL as the number of groups
        launched by the OpenCL kernel (if the user has not specified a value)

        Returns
        -------
        premables: list of str
            The string preambles for this :class:`kernel_generator`
        """

        if self.user_specified_work_size:
            return []

        work_size = """
        #ifndef work_size
            #define work_size (get_num_groups())
        #endif
        """

        return [work_size]

    def _get_pointer_unpack(self, array, offset):
        """
        Implement the pattern
        ```
            double* array = &rwk[offset]
        ```
        for OpenCL

        Parameters
        ----------
        array: str
            The array name
        offset: str
            The stringified offset

        Returns
        -------
        unpack: str
            The stringified pointer unpacking statement
        """

        return '__global double* {} = rwk + {};'.format(array, offset)

    def _special_kernel_subs(self, file_src):
        """
        An override of the :method:`kernel_generator._special_kernel_subs`
        that implements OpenCL specific kernel substitutions

        Parameters
        ----------
        file_src : Template
            The kernel source template to substitute into

        Returns
        -------
        new_file_src : str
            An updated kernel source string to substitute general template
            parameters into
        """

        # open cl specific
        # vec width
        vec_width = self.vec_width
        if not vec_width:
            # set to default
            vec_width = 1
        # platform
        platform_str = self.loopy_opts.platform.get_info(
            cl.platform_info.VENDOR)
        # build options
        build_options = self.build_options
        # kernel arg setting
        kernel_arg_set = self.get_kernel_arg_setting()
        # kernel list
        kernel_paths = [self.bin_name]
        kernel_paths = ', '.join('"{}"'.format(x)
                                 for x in kernel_paths if x.strip())

        # find maximum size of device arrays (that are allocated per-run)
        p_var = p_size.name
        # filter arrays to those depending on problem size
        arrays = [a for a in self.mem.arrays if any(
            p_var in str(x) for x in a.shape)]
        # next convert to size
        arrays = [np.prod(np.fromstring(
            self.mem._get_size(a, subs_n='1'), dtype=arc.kint_type, sep=' * '))
            for a in arrays]
        # and get max size
        max_size = str(max(arrays)) + ' * {}'.format(
            self.arg_name_maps[p_size])

        # find converted constant variables -> global args
        host_constants = self.mem.get_host_constants()
        host_constants_transfers = self.mem.get_host_constants_in()

        # get host memory syncs if necessary
        mem_strat = self.mem.get_mem_strategy()

        return subs_at_indent(file_src,
                              vec_width=vec_width,
                              platform_str=platform_str,
                              build_options=build_options,
                              kernel_arg_set=kernel_arg_set,
                              kernel_paths=kernel_paths,
                              device_type=str(self.loopy_opts.device_type),
                              num_source=1,  # only 1 program / binary is built
                              CL_LEVEL=int(float(self._get_cl_level()) * 100),  # noqa -- CL standard level
                              max_size=max_size,  # max size for CL1.1 mem init
                              host_constants=host_constants,
                              host_constants_transfers=host_constants_transfers,
                              MEM_STRATEGY=mem_strat
                              )

    def get_kernel_arg_setting(self):
        """
        Needed for OpenCL, this generates the code that sets the kernel args

        Parameters
        ----------
        None

        Returns
        -------
        knl_arg_set_str : str
            The code that sets opencl kernel args
        """

        kernel_arg_sets = []
        for i, arg in enumerate(self.kernel_data):
            if not isinstance(arg, lp.ValueArg):
                kernel_arg_sets.append(
                    self.set_knl_arg_array_template.safe_substitute(
                        arg_index=i,
                        arg_size='sizeof({})'.format('d_' + arg.name),
                        arg_value='&d_' + arg.name)
                )
            else:
                # workaround for integer overflow of cl_uint
                # TODO: need to put in detection for integer overlflow here
                # or at least limits for maximum size of kernel before we switch
                # over to a 64bit integer for index type
                name = arg.name if arg not in self.arg_name_maps else \
                    self.arg_name_maps[arg]
                arg_set = self.set_knl_arg_value_template.safe_substitute(
                        arg_index=i,
                        arg_size='sizeof({})'.format(self.type_map[arg.dtype]),
                        arg_value='&{}'.format(name))
                kernel_arg_sets.append(arg_set)

        return '\n'.join(kernel_arg_sets)

    def _get_cl_level(self):
        """
        Searches the supplied platform for a OpenCL level.  If not found,
        uses the level from the site config

        Parameters
        ----------
        None

        Returns
        -------
        cl_level: str
            The stringified OpenCL standard level
        """

        # try get the platform's CL level
        try:
            device_level = self.loopy_opts.device.opencl_c_version.split()
            for d in device_level:
                try:
                    float(d)
                    return d
                    break
                except ValueError:
                    pass
        except AttributeError:
            # default to the site level
            return site.CL_VERSION

    def _generate_compiling_program(self, path, filename):
        """
        Needed for OpenCL, this generates a simple C file that
        compiles and stores the binary OpenCL kernel generated w/ the wrapper

        Parameters
        ----------
        path : str
            The output path to write files to
        filename : str
            The filename of the wrapping kernel

        Returns
        -------
        None
        """

        assert filename, (
            'Cannot generate compiler before wrapping kernel is generated...')
        if self.depends_on:
            assert [x.filename for x in self.depends_on], (
                'Cannot generate compiler before wrapping kernel '
                'for dependencies are generated...')

        self.build_options = ''
        if self.lang == 'opencl':
            with open(os.path.join(script_dir, self.lang,
                                   'opencl_kernel_compiler.c.in'),
                      'r') as file:
                file_str = file.read()
                file_src = Template(file_str)

            # get the platform from the options
            if self.loopy_opts.platform_is_pyopencl:
                platform_str = self.loopy_opts.platform.get_info(
                    cl.platform_info.VENDOR)
            else:
                logger = logging.getLogger(__name__)
                logger.warn('OpenCL platform name "{}" could not be checked as '
                            'PyOpenCL not found, using user supplied platform '
                            'name.'.format(self.loopy_opts.platform_name))
                platform_str = self.loopy_opts.platform_name

            cl_std = self._get_cl_level()

            # for the build options, we turn to the siteconf
            self.build_options = ['-I' + x for x in site.CL_INC_DIR + [path]]
            self.build_options.extend(site.CL_FLAGS)
            self.build_options.append('-cl-std=CL{}'.format(cl_std))
            self.build_options = ' '.join(self.build_options)

            file_list = [filename]
            file_list = ', '.join('"{}"'.format(x) for x in file_list)

            self.bin_name = filename[:filename.index(
                utils.file_ext[self.lang])] + '.bin'

            with filew.get_file(os.path.join(path, self.name + '_compiler'
                                             + utils.file_ext[self.lang]),
                                self.lang, use_filter=False) as file:
                file.add_lines(file_src.safe_substitute(
                    filenames=file_list,
                    outname=self.bin_name,
                    platform=platform_str,
                    build_options=self.build_options,
                    # compiler expects all source strings
                    num_source=1
                ))

    def apply_barriers(self, instructions):
        """
        An override of :method:`kernel_generator.apply_barriers` that
        applies synchronization barriers to OpenCL kernels

        Parameters
        ----------

        instructions: list of str
            The instructions for this kernel
        Returns
        -------

        synchronized_instructions : list of str
            The instruction list with the barriers inserted
        """

        barriers = self.barriers[:]
        instructions = list(enumerate(instructions))
        for barrier in barriers:
            # find insert index (the second barrier ind)
            index = next(ind for ind, inst in enumerate(instructions)
                         if inst[0] == barrier[1])
            # check that we're inserting between the required barriers
            assert barrier[0] == instructions[index - 1][0]
            # and insert
            instructions.insert(index, (-1, self.barrier_templates[barrier[2]]
                                        + utils.line_end[self.lang]))
        # and get rid of indicies
        instructions = [inst[1] for inst in instructions]
        return instructions

    @property
    def hoist_locals(self):
        """
        In OpenCL we need to strip out any declaration of a __local variable in
        subkernels, as these must be defined in the called in the kernel scope

        This entails hoisting local declarations up to the wrapping
        kernel for non-separated OpenCL kernels as __local variables in
        sub-functions are not well defined in the standard:
        https://www.khronos.org/registry/OpenCL/sdk/1.2/docs/man/xhtml/functionQualifiers.html # noqa
        """

        return True


class knl_info(object):

    """
    A composite class that contains the various parameters, etc.
    needed to create a simple kernel

    name : str
        The kernel name
    instructions : str or list of str
        The kernel instructions
    mapstore : :class:`array_creator.MapStore`
        The MapStore object containing map domains, indicies, etc.
    pre_instructions : list of str
        The instructions to execute before the inner loop
    post_instructions : list of str
        The instructions to execute after end of inner loop but before end
        of outer loop
    var_name : str
        The inner loop variable
    kernel_data : list of :class:`loopy.ArrayBase`
        The arguements / temporary variables for this kernel
    extra_inames : list of tuple
        A list of (iname, domain) tuples the form the extra loops in this kernel
    assumptions : list of str
        Assumptions to pass to the loopy kernel
    parameters : dict
        Dictionary of parameter values to fix in the loopy kernel
    extra subs : dict
        Dictionary of extra string substitutions to make in kernel generation
    can_vectorize : bool
        If False, the vectorization specializer must be used to vectorize this kernel
    vectorization_specializer : function
        If specified, use this specialization function to fix problems that would
        arise in vectorization
    preambles : :class:`preamble.PreambleGen`
        A list of preamble generators to insert code into loopy / opencl
    **kwargs: dict
        Any other keyword args to pass to :func:`loopy.make_kernel`
    """

    def __init__(self, name, instructions, mapstore, pre_instructions=[],
                 post_instructions=[],
                 var_name='i', kernel_data=None,
                 extra_inames=[],
                 assumptions=[], parameters={},
                 extra_subs={},
                 vectorization_specializer=None,
                 can_vectorize=True,
                 manglers=[],
                 preambles=[],
                 iname_domain_override=[],
                 **kwargs):

        def __listify(arr):
            if isinstance(arr, str):
                return [arr]
            return arr
        self.name = name
        self.instructions = instructions
        self.mapstore = mapstore
        self.pre_instructions = __listify(pre_instructions)[:]
        self.post_instructions = __listify(post_instructions)[:]
        self.var_name = var_name
        if isinstance(kernel_data, set):
            kernel_data = list(kernel_data)
        self.kernel_data = kernel_data[:]
        self.extra_inames = extra_inames[:]
        self.assumptions = assumptions[:]
        self.parameters = parameters.copy()
        self.extra_subs = extra_subs
        self.can_vectorize = can_vectorize
        self.vectorization_specializer = vectorization_specializer
        self.manglers = manglers[:]
        self.preambles = preambles[:]
        self.iname_domain_override = iname_domain_override[:]
        self.kwargs = kwargs.copy()


def create_function_mangler(kernel, return_dtypes=()):
    """
    Returns a function mangler to interface loopy kernels with function calls
    to other kernels (e.g. falloff rates from the rate kernel, etc.)

    Parameters
    ----------
    kernel : :class:`loopy.LoopKernel`
        The kernel to create an interface for
    return_dtypes : list :class:`numpy.dtype` returned from the kernel, optional
        Most likely an empty list
    Returns
    -------
    func : :method:`MangleGen`.__call__
        A function that will return a :class:`loopy.kernel.data.CallMangleInfo` to
        interface with the calling :class:`loopy.LoopKernel`
    """
    from ..loopy_utils.preambles_and_manglers import MangleGen

    dtypes = []
    for arg in kernel.args:
        if not isinstance(arg, lp.TemporaryVariable):
            dtypes.append(arg.dtype)
    mg = MangleGen(kernel.name, tuple(dtypes), return_dtypes)
    return mg.__call__


def _find_indent(template_str, key, value):
    """
    Finds and returns a formatted value containing the appropriate
    whitespace to put 'value' in place of 'key' for template_str

    Parameters
    ----------
    template_str : str
        The string to sub into
    key : str
        The key in the template string
    value : str
        The string to format

    Returns
    -------
    formatted_value : str
        The properly indented value
    """

    # find the instance of ${key} in kernel_str
    whitespace = None
    for i, line in enumerate(template_str.split('\n')):
        if key in line:
            # get whitespace
            whitespace = re.match(r'\s*', line).group()
            break
    result = [line if i == 0 else whitespace + line for i, line in
              enumerate(textwrap.dedent(value).splitlines())]
    return '\n'.join(result)


def subs_at_indent(template_str, **kwargs):
    """
    Substitutes keys of :params:`kwargs` for values in :param:`template_str`
    ensuring that the indentation of the value is the same as that of the key
    for all lines present in the value

    Parameters
    ----------
    template_str : str
        The string to sub into
    kwargs: dict
        The dictionary of keys -> values to substituted into the template
    Returns
    -------
    formatted_value : str
        The formatted string
    """

    return Template(template_str).safe_substitute(
        **{key: _find_indent(template_str, '${{{key}}}'.format(key=key),
                             value if isinstance(value, str) else str(value))
            for key, value in six.iteritems(kwargs)})
