# -----------------------------------------------------------------------------
# Copyright (c) 2014--, The Qiita Development Team.
#
# Distributed under the terms of the BSD 3-clause License.
#
# The full license is in the file LICENSE, distributed with this software.
# -----------------------------------------------------------------------------

from os import mkdir, environ
from os.path import join, exists, abspath, split
from shutil import which
from pkg_resources import Requirement, resource_filename

from future.utils import viewitems
from functools import partial
from collections import OrderedDict
import json

from biom import Table, load_table
from biom.util import biom_open
from qiita_client import ArtifactInfo
from qiita_client.util import system_call

from qiita_files.demux import to_per_sample_files


DEBLUR_PARAMS = {
    'Positive filtering database': 'pos-ref-fp',
    'Negative filtering database': 'neg-ref-fp',
    'Indexed positive filtering database': 'pos-ref-db-fp',
    'Indexed negative filtering database': 'neg-ref-db-fp',
    'Mean per nucleotide error rate': 'mean-error',
    'Error probabilities for each Hamming distance': 'error-dist',
    'Insertion/deletion (indel) probability': 'indel-prob',
    'Maximum number of insertion/deletion (indel)': 'indel-max',
    'Sequence trim length (-1 for no trimming)': 'trim-length',
    'Minimum dataset-wide read threshold': 'min-reads',
    'Minimum per-sample read threshold': 'min-size',
    'Threads per sample': 'threads-per-sample',
    'Jobs to start': 'jobs-to-start'}


def generate_deblur_workflow_commands(preprocessed_fp, out_dir, parameters):
    """Generates the deblur commands

    Parameters
    ----------
    preprocessed_fp : list of str
        A list of 1 element with the input fastq or per sample folder
    out_dir : str
        The job output directory
    parameters : dict
        The command's parameters, keyed by parameter name

    Returns
    -------
    list of str
        The deblur commands

    Raises
    ------
    ValueError
        If there is more than 1 file passed as preprocessed_fastq
    """
    if len(preprocessed_fp) != 1:
        raise ValueError("deblur doesn't accept more than one filepath: "
                         "%s" % ', '.join(preprocessed_fp))

    translated_params = {DEBLUR_PARAMS[k]: v for k, v in parameters.items()}
    params = OrderedDict(sorted(translated_params.items(), key=lambda t: t[0]))
    params = ['--%s "%s"' % (k, v) if v is not True else '--%s' % k
              for k, v in viewitems(params) if v != 'default']
    cmd = 'deblur workflow --seqs-fp "%s" --output-dir "%s" %s' % (
        preprocessed_fp[0], out_dir, ' '.join(params))

    return cmd


def generate_sepp_placements(seqs, out_dir, threads, reference_phylogeny=None,
                             reference_alignment=None):
    """Generates the sepp commands

    Parameters
    ----------
    seqs : list of str
        A list of seqs to generate placements
    out_dir : str
        The job output directory
    threads : int
        Number if CPU cores to use
    reference_phylogeny : str, optional
        A filepath to an alternative reference phylogeny for SEPP.
        If None, default phylogeny is uses, which is Greengenes 13.8 99% id.
    reference_alignment : str, optional
        A filepath to an alternative reference alignment for SEPP.
        If None, default alignment is uses, which is Greengenes 13.8 99% id.

    Returns
    -------
    dict of strings
        keys are the seqs, values are the new placements as JSON strings

    Raises
    ------
    ValueError
        If run-sepp.sh does not produce expected file placements.json which is
        an indicator that something failed.
    """
    # return an empty dict if no sequences have been passed to the function
    if len(seqs) < 1:
        return {}

    # Create a multiple fasta file for all input seqs
    file_input = "%s/input.fasta" % out_dir
    with open(file_input, 'w') as fh_input:
        for seq in seqs:
            fh_input.write(">%s\n%s\n" % (seq, seq))

    # execute SEPP
    run_name = 'qiita'
    param_phylogeny = ''
    if reference_phylogeny is not None:
        param_phylogeny = ' -t %s ' % reference_phylogeny
    param_alignment = ''
    if reference_alignment is not None:
        param_alignment = ' -a %s ' % reference_alignment
    # SEPP writes output into the current working directory (cwd), therefore
    # we here first need to store the cwd, then move into the output directory,
    # perform SEPP and move back to the stored cwd for a clean state
    curr_pwd = environ['PWD']
    std_out, std_err, return_value = system_call(
        'cd %s && run-sepp.sh %s %s -x %i %s %s; cd %s' %
        (out_dir, file_input, run_name, threads,
         param_phylogeny, param_alignment, curr_pwd))

    # parse placements from SEPP results
    file_placements = '%s/%s_placement.json' % (out_dir, run_name)
    if exists(file_placements):
        with open(file_placements, 'r') as fh_placements:
            plcmnts = json.loads(fh_placements.read())
            return {p['nm'][0][0]: p['p'] for p in plcmnts['placements']}
    else:
        # due to the wrapper style of run-sepp.sh the actual exit code is never
        # returned and we have no way of finding out which sub-command failed
        # Therefore, we can only assume that something went wrong by not
        # observing the expected output file.
        # If the main SEPP program fails, it reports some information in two
        # files, whoes content we can read and report
        file_stderr = '%s/sepp-%s-err.log' % (out_dir, run_name)
        if exists(file_stderr):
            with open(file_stderr, 'r') as fh_stderr:
                std_err = fh_stderr.readlines()
        file_stdout = '%s/sepp-%s-out.log' % (out_dir, run_name)
        if exists(file_stdout):
            with open(file_stdout, 'r') as fh_stdout:
                std_out = fh_stdout.readlines()
        error_msg = ("Error running run-sepp.sh:\nStd out: %s\nStd err: %s"
                     % (std_out, std_err))
        raise ValueError(error_msg)


def _get_guppy_binary():
    """Find the right guppy binary.

    Returns
    -------
    str : filepath of the guppy binary.

    Raises
    ------
    ValueError if
        a) grep'ing DIR in the run-sepp.sh script or
        b) grep'ing pplacer path in main.config fails
    """
    # 1) locate sepp binary
    fp_sepp_binary = which('run-sepp.sh')
    # 2) obtain shared directory by grep'ing DIR from the run-sepp.sh script
    std_out, _, return_value = system_call('grep "^DIR=" %s' % fp_sepp_binary)
    if return_value != 0:
        raise ValueError("Could not determine SEPP shared directory.")
    fp_sepp_shared = std_out.split('=')[-1].strip()
    # 3) grep'ing pplacer path
    std_out, _, return_value = system_call(
        'grep "\[pplacer\]" %s -A 1 | grep "path"' %
        join(fp_sepp_shared, 'sepp', '.sepp', 'main.config'))
    if return_value != 0:
        raise ValueError("Could not determine SEPP bundled tool directory.")
    fp_sepp_bundled = split(std_out.split('=')[-1].strip())[0]
    return join(fp_sepp_bundled, 'guppy')


def generate_insertion_trees(placements, out_dir,
                             reference_template=None,
                             reference_rename=None):
    """Generates phylogenetic trees by inserting placements into a reference

    Parameters
    ----------
    placements : dict of strings
        keys are the seqs, values are the new placements as JSON strings
    out_dir : str
        The job output directory
    reference_template : str, optional
        Filepath to the reference placement json file. This file is the result
        of a run-sepp.sh run with at least one sequence, but placements are
        manually removed, i.e. the placements field is the empty list []. Make
        sure that the template is in sync with the reference that was used to
        generate the placements!
        If None, it falls back to the Greengenes 13.8 99% reference.
    reference_rename : str, optional
        Similar to reference_template, but a filepath to the generated python
        renaming script to undo the name scaping post guppy.
        If None, it falls back to the Greengenes 13.8 99% reference.

    Returns
    -------
    str
        The phylogenetic insertion tree in Newick format.
    """

    # create a valid placement.json file as input for guppy
    file_ref_template = resource_filename(
        Requirement.parse('qp_deblur'),
        'qp_deblur/assets/tmpl_gg13.8-99_placement.json')
    if reference_template is not None:
        file_ref_template = reference_template
    if not exists(file_ref_template):
        raise ValueError("Reference template '%s' does not exits!" %
                         file_ref_template)
    with open(file_ref_template, 'r') as f:
        plcmnts = json.loads(f.read())
        for sequence, placement in placements.items():
            plcmnts['placements'].append({'p': placement,
                                          'nm': [[sequence, 1]]})
    file_placements = '%s/placements.json' % out_dir
    with open(file_placements, 'w') as f:
        json.dump(plcmnts, f)

    # execute guppy
    file_tree_escaped = join(out_dir, 'insertion_tree.tre')
    fp_guppy = _get_guppy_binary()
    std_out, std_err, return_value = system_call(
        '%s tog %s -o %s' % (fp_guppy, file_placements, file_tree_escaped))
    if return_value != 0:
        error_msg = ("Error running guppy:\nStd out: %s\nStd err: %s"
                     % (std_out, std_err))
        raise ValueError(error_msg)

    # execute node name re-labeling (to revert the escaping of names necessary
    # for guppy)
    file_ref_rename = resource_filename(
        Requirement.parse('qp_deblur'),
        'qp_deblur/assets/tmpl_gg13.8-99_rename-json.py')
    if reference_rename is not None:
        file_ref_rename = reference_rename
    if not exists(file_ref_rename):
        raise ValueError("Reference rename script does not exits!")
    file_tree = join(out_dir, 'insertion_tree.relabelled.tre')
    std_out, std_err, return_value = system_call(
        'cat %s | python %s > %s' %
        (file_tree_escaped, file_ref_rename, file_tree))
    if return_value != 0:
        error_msg = (("Error running %s:\n"
                      "Std out: %s\nStd err: %s")
                     % (file_ref_rename, std_out, std_err))
        raise ValueError(error_msg)

    with open(file_tree, 'r') as f:
        return "".join(f.readlines())


def deblur(qclient, job_id, parameters, out_dir):
    """Run deblur with the given parameters

    Parameters
    ----------
    qclient : qiita_client.QiitaClient
        The Qiita server client
    job_id : str
        The job id
    parameters : dict
        The parameter values to run deblur
    out_dir : str
        The path to the job's output directory

    Returns
    -------
    boolean, list, str
        The results of the job

    Notes
    -----
    The code will check if the artifact has a preprocessed_demux element, if
    not it will use the preprocessed_fastq. We prefer to work with the
    preprocessed_demux as running time will be greatly improved
    """
    out_dir = join(out_dir, 'deblur_out')
    # Step 1 get the rest of the information need to run deblur
    qclient.update_job_step(job_id, "Step 1 of 4: Collecting information")
    artifact_id = parameters['Demultiplexed sequences']
    # removing input from parameters so it's not part of the final command
    del parameters['Demultiplexed sequences']

    # Get the artifact filepath information
    artifact_info = qclient.get("/qiita_db/artifacts/%s/" % artifact_id)
    fps = artifact_info['files']

    # Step 2 generating command deblur
    if 'preprocessed_demux' in fps:
        qclient.update_job_step(job_id, "Step 2 of 4: Generating per sample "
                                "from demux (1/2)")

        if not exists(out_dir):
            mkdir(out_dir)
        split_out_dir = join(out_dir, 'split')
        if not exists(split_out_dir):
            mkdir(split_out_dir)

        # using the same number of parallel jobs as defined by the command
        n_jobs = int(parameters['Jobs to start'])
        # [0] cause there should be only 1 file
        to_per_sample_files(fps['preprocessed_demux'][0],
                            out_dir=split_out_dir, n_jobs=n_jobs)

        qclient.update_job_step(job_id, "Step 2 of 4: Generating per sample "
                                "from demux (2/2)")
        out_dir = join(out_dir, 'deblured')
        cmd = generate_deblur_workflow_commands([split_out_dir],
                                                out_dir, parameters)
    else:
        qclient.update_job_step(job_id, "Step 2 of 4: Generating deblur "
                                "command")
        cmd = generate_deblur_workflow_commands(fps['preprocessed_fastq'],
                                                out_dir, parameters)

    # Step 3 execute deblur
    qclient.update_job_step(job_id, "Step 3 of 4: Executing deblur job")
    std_out, std_err, return_value = system_call(cmd)
    if return_value != 0:
        error_msg = ("Error running deblur:\nStd out: %s\nStd err: %s"
                     % (std_out, std_err))
        return False, None, error_msg

    # Generating artifact
    pb = partial(join, out_dir)

    # Generate the filepaths
    final_biom = pb('all.biom')
    final_seqs = pb('all.seqs.fa')
    final_biom_hit = pb('reference-hit.biom')
    final_seqs_hit = pb('reference-hit.seqs.fa')

    if not exists(final_biom_hit):
        # Create an empty table. We need to send something to Qiita that is
        # a valid BIOM, so we are going to create an empty table
        t = Table([], [], [])
        with biom_open(final_biom_hit, 'w') as f:
            t.to_hdf5(f, 'qp-deblur generated')

    if not exists(final_seqs_hit):
        # Same as before, create an empty sequence file so we can send it
        with open(final_seqs_hit, 'w') as f:
            f.write("")

    # Step 4, communicate with archive to check and generate placements
    qclient.update_job_step(job_id, "Step 4 of 4 (1/2): Retriving "
                            "observations information")
    features = list(load_table(final_biom_hit).ids(axis='observation'))
    if features:
        observations = qclient.post(
            "/qiita_db/archive/observations/", data={'job_id': job_id,
                                                     'features': features})
        no_placements = [k for k, v in observations.items() if v == '']
        qclient.update_job_step(job_id, "Step 4 of 4 (2/2): Generating %d new "
                                "placements" % len(no_placements))
        try:
            new_placements = generate_sepp_placements(
                no_placements, out_dir, parameters['Threads per sample'])
        except ValueError as e:
            return False, None, str(e)
    else:
        new_placements = None

    ainfo = [ArtifactInfo('deblur final table', 'BIOM',
                          [(final_biom, 'biom'),
                           (final_seqs, 'preprocessed_fasta')]),
             ArtifactInfo('deblur reference hit table', 'BIOM',
                          [(final_biom_hit, 'biom'),
                           (final_seqs_hit, 'preprocessed_fasta')],
                          new_placements)]

    return True, ainfo, ""
