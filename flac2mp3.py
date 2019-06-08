#!/usr/bin/env python

import itertools
import multiprocessing as mp
import os
import re
import shutil
import subprocess as sp
import sys
import tempfile

def get_missing_programs(required_programs):
    '''Gets a list of required programs that can't be found on the system.'''

    # try to launch the programs, and add them to a list if they're not found
    missing = []
    for program in required_programs:
        try:
            sp.call(program, stdout=sp.PIPE, stderr=sp.STDOUT)
        except OSError as e:
            # if the binary couldn't be found, put it in the list
            if e.errno == 2:
                missing.append(program)
            else:
                # propogate other errors
                raise

    return missing

def ensure_directory(d, ignore_errors=False):
    '''
    Given a directory, ensures that it exists by creating the directory tree if
    it's not already present. Returns True if the directory was created, False
    if it already exists.
    '''

    try:
        os.makedirs(d)
        return True
    except OSError as e:
        # propogate the error if it DOESN'T indicate that the directory already
        # exists.
        if e.errno != 17 and not ignore_errors:
            raise e
        return False

def change_file_ext(fname, ext):
    '''Transforms the given filename's extension to the given extension.'''
    return os.path.splitext(fname)[0] + ext

def walk_dir(d, follow_links=False):
    '''
    Yields all the file names in a given directory, including those in
    subdirectories.  If 'follow_links' is True, symbolic links will be followed.
    This option can lead to infinite looping since the function doesn't keep
    track of which directories have been visited.
    '''

    # walk the directory and collect the full path of every file therein
    for root, dirs, files in os.walk(d, followlinks=follow_links):
        for name in files:
            # append the normalized file name
            yield os.path.abspath(os.path.join(root, name))

def walk_paths(path_list, follow_links=False):
    '''
    Yields all the file names in a given list of files and directories
    If 'follow_links' is True, symbolic links will be followed.

    Files are guaranteed to be listed only once
    '''

    # keep track of files added to de-duplicate files supplied on command line and avoid
    # symlink cycles
    files = set()

    for p in path_list:
        if os.path.isdir(p):
            for f in walk_dir(p):
                if f not in files:
                    files.add(f)
                    yield f
        else:
            if p not in files:
                files.add(p)
                yield p

def get_filetype(fname):
    '''Takes a file name and returns its MIME type.'''

    # brief output, MIME version
    file_args = ['file', '-b']
    if sys.platform == 'darwin':
        file_args.append('-I')
    else:
        file_args.append('-i')
    file_args.append(fname)

    # return one item per line
    p_file = sp.Popen(file_args, stdout=sp.PIPE)
    return p_file.communicate()[0].strip()


def get_encoder_options(preset, vbr_quality):
    '''
    Construct lame command line options
    '''
    if preset:
        # specify lame preset
        return [ '--preset', preset ]
    if vbr_quality:
        # specify lame -Vn VBR quality setting
        return [ '-q0', '-V' + str(vbr_quality) ]
    # defaults: highest quality
    return [ '-q0', '-V0' ]


def transcode(infile, outfile=None, skip_existing=False, bad_chars='', encoder_options=[]):
    '''
    Transcodes a single flac file into a single mp3 file.  Preserves the file
    name but changes the extension.  Copies flac tag info from the original file
    to the transcoded file. If outfile is specified, the file is saved to that
    location, otherwise it's saved alongside the original file. If skip_existing
    is False (the default), overwrites existing files with the same name as
    outfile, otherwise skips the file completely. bad_chars is a collection of
    characters that should be removed from the output file name.  Returns the
    returncode of the lame process.
    '''

    # get a new file name for the mp3 if no output name was specified
    outfile = outfile or change_file_ext(infile, '.mp3')

    # replace incompatible filename characters in output file
    for c in bad_chars:
        outfile = outfile.replace(c, '')

    # skip transcoding existing files if specified
    if skip_existing and os.path.exists(outfile):
        return

    # NOTE: we use a temp file to store the incremental in-flight transcode, and
    # move it to the final output filename when transcode is complete. this
    # approach prevents partial or interrupted transcodes from getting in the
    # way of --skip-existing.

    # create the file in the same dir (and same filesystem) as the final target,
    # this keeps our final shutil.move efficient
    dirname = os.path.dirname(outfile)
    with tempfile.NamedTemporaryFile(dir=dirname, suffix='.tmp') as temp_outfile:
        # get the tags from the input file
        flac_tags = get_tags(infile)

        # arguments for 'lame', including encoder quality options, and tag values
        lame_args = ['lame' ] + encoder_options + [
                '--add-id3v2', '--silent',
                '--tt', flac_tags['TITLE'],
                '--ta', flac_tags['ARTIST'],
                '--tl', flac_tags['ALBUM'],
                '--ty', flac_tags['DATE'],
                '--tc', flac_tags['COMMENT'],
                '--tn', flac_tags['TRACKNUMBER'] + '/' + flac_tags['TRACKTOTAL'],
                '--tg', flac_tags['GENRE'],
                '-', '-' ]

        # arguments for 'flac' decoding to be piped to 'lame'
        flac_args = ['flac', '--silent', '--stdout', '--decode', infile]

        # decode the 'flac' data and pass it to 'lame'
        # pass the lame encoding to our temp file
        p_flac = sp.Popen(flac_args, stdout=sp.PIPE)
        p_lame = sp.Popen(lame_args, stdin=p_flac.stdout, stdout=temp_outfile)

        # allow p_flac to receive a SIGPIPE if p_lame exits
        p_flac.stdout.close()

        p_flac_retval = p_flac.wait()
        # wait for the encoding to finish
        p_lame_retval = p_lame.wait()

        # if the transcode worked, link the temp file to the final filename
        if p_lame_retval == 0 and p_flac_retval == 0:
            shutil.move(temp_outfile.name, outfile)
            # we're keeping this temp file.  Don't delete it
            temp_outfile.delete = False

    return p_flac_retval or p_lame_retval

def get_tags(infile):
    '''
    Gets the flac tags from the given file and returns them as a dict.  Ensures
    a minimun set of id3v2 tags is available, giving them default values if
    these tags aren't found in the orininal file.
    '''

    # get tag info text using 'metaflac'
    metaflac_args = ['metaflac', '--list', '--block-type=VORBIS_COMMENT', infile]
    p_metaflac = sp.Popen(metaflac_args, stdout=sp.PIPE)
    metaflac_text = p_metaflac.communicate()[0]

    # ensure all possible id3v2 tags start off with a default value
    tag_dict = {
        'TITLE': 'NONE',
        'ARTIST': 'NONE',
        'ALBUM': 'NONE',
        'DATE': '1',
        'COMMENT': '',
        'TRACKNUMBER': '00',
        'TRACKTOTAL': '00',
        'GENRE': 'NONE'
    }

    # matches all lines like 'comment[0]: TITLE=Misery' and extracts them to
    # tuples like ('TITLE', 'Misery'), then stores them in a dict.
    pattern = '\s+comment\[\d+\]:\s+([^=]+)=([^\n]+)\n'

    # get the comment data from the obtained text
    for name, value in re.findall(pattern, metaflac_text):
        tag_dict[name.upper()] = value

    return tag_dict

def lines_from_file(file):
    '''
    A generator to read lines from a file that does not buffer the input lines

    Standard python file iterator line-buffering prevents interactive uses of stdin as
    an input file.  Only readline() has the line-buffered behaviour we want.
    '''
    while file and True:
        line = file.readline()
        if line == '':
            break
        yield line.strip()

if __name__ == '__main__':
    import logging
    import time
    import argparse

    # parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('files', metavar='FILES', type=str, nargs='*',
            help='Files and/or directories to transcode')

    # options and flags
    parser.add_argument('-o', '--output-dir', type=os.path.abspath,
            help='Directory to output transcoded files to')
    parser.add_argument('-d', '--root-dir', type=os.path.abspath,
            help='Root directory containing your source media.  Preserve directory ' +
            'structure from this point in --output-dir')
    parser.add_argument('-f', '--file', nargs='?', type=argparse.FileType('r'), 
            default=None, dest='input_file',
            help='Supply a list of files to transcode in FILE')
    parser.add_argument('-s', '--skip-existing', action='store_true',
            help='Skip transcoding files if the output file already exists')
    parser.add_argument('-l', '--logfile', type=os.path.normpath, default=None,
            help='log output to a file as well as to the console.')
    parser.add_argument('-q', '--quiet', action='store_true',
            help='Disable console output.')
    parser.add_argument('-V', type=int, default='2', dest='vbr_quality',
            help='VBR quality setting passed through to lame command line')
    parser.add_argument('--preset', default=None,
            help='lame preset setting passed through to lame command line')
    parser.add_argument('-c', '--copy-pattern', type=re.compile,
            help="Copy files who's names match the given pattern into the " +
            'output directory. Only works if an output directory is specified.')
    parser.add_argument('-n', '--num-threads', type=int, default=mp.cpu_count(),
            help='The number of threads to use for transcoding. Defaults ' +
            'to the number of CPUs on the machine.')
    args = parser.parse_args()

    # set log level and format
    log = logging.getLogger('flac2mp3')
    log.setLevel(logging.INFO)

    # prevent 'no loggers found' warning
    log.addHandler(logging.NullHandler())

    # custom log formatting
    formatter = logging.Formatter('[%(levelname)s] %(message)s')

    # log to stderr unless disabled
    if not args.quiet:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        log.addHandler(sh)

    # add a file handler if specified
    if args.logfile is not None:
        fh = logging.FileHandler(args.logfile)
        fh.setFormatter(formatter)
        log.addHandler(fh)

    # ensure we have all our required programs
    missing = get_missing_programs(['lame', 'file', 'flac', 'metaflac'])
    if len(missing) > 0:
        log.critical('The following programs are required: ' + ','.join(missing))
        sys.exit(1)

    # ensure the output directory exists
    if args.output_dir is not None:
        try:
            ensure_directory(args.output_dir)
        except OSError as e:
            log.error("Couldn't create directory '%s'" % args.output_dir)

    # get the common prefix of all the files so we can preserve directory
    # structure when an output directory is specified.
    if args.root_dir:
        # use the source root directory provided on the command line
        common_prefix = args.root_dir
    else:
        # ...or detect the common prefix from the input file list
        log.info('Enumerating files...')
        files = list( walk_paths( itertools.chain(
                            args.files,
                            lines_from_file( args.input_file ) ) ) )
        log.info('Found ' + str(len(files)) + ' files')
        common_prefix = os.path.dirname(os.path.commonprefix(files))

    def transcode_with_logging(f):
        '''Transcode the given file and print out progress statistics.'''

        # copy any non-FLAC files to the output dir if they match a pattern
        if 'audio/x-flac' not in get_filetype(f):
            if args.output_dir is not None and args.copy_pattern is not None:
                match = args.copy_pattern.search(f)
                if match is not None:
                    dest = os.path.join(args.output_dir,
                            f.replace(common_prefix, '').strip('/'))
                    try:
                        ensure_directory(os.path.dirname(dest))
                        shutil.copy(f, dest)
                        log.info("Copied '%s' ('%s' matched)", f,
                                match.group(0))
                    except Exception, e:
                        log.error("Failed to copy '%s' (%s)", f,
                                e.message)

                    # we're done once we've attempted a copy
                    return

            log.info("Skipped '%s'", f)

            # never proceed further if the file wasn't a FLAC file
            return

        # a more compact file name representation
        log.info("Transcoding '%s'..." % f)

        # time the transcode
        start_time = time.time()

        # assign the output directory
        outfile = None
        if args.output_dir is not None:
            mp3file = change_file_ext(f, '.mp3')
            outfile = os.path.join(args.output_dir,
                    mp3file.replace(common_prefix, '').strip('/'))

            # make the directory to ensure it exists.
            ensure_directory(os.path.dirname(outfile))

        encoder_options = get_encoder_options( args.preset, args.vbr_quality )
        # store the return code of the process so we can see if it errored
        retcode = transcode(f, outfile, args.skip_existing, ':', encoder_options)
        total_time = time.time() - start_time

        # log success or error
        if retcode == 0:
            log.info("Transcoded '%s' in %.2f seconds" % (f,
                total_time))
        elif retcode is None:
            log.info("Skipped '%s'", f)
        else:
            log.error("Failed to transcode '%s' after %.2f seconds" %
                    (f, total_time))

    # log transcode status
    log.info('Beginning transcode...')
    overall_start_time = time.time()

    # build a thread pool for transcoding
    pool = mp.Pool(processes=args.num_threads)

    # transcode all the found files
    terminated = False
    succeeded = False
    pending_results = []
    try:
        # iterate over the paths listed on the command line followed by any files listed
        # in file_input
        # Use of lines_from_file() gives us nice line-by-line interactive behaviour if
        # input file is stdin.  Use of itertools to chain the generators gives us
        # lazy evaluation so we don't wait for EOF to starting transcoding jobs.
        for f in walk_paths( itertools.chain(
                            args.files,
                            lines_from_file( args.input_file ) ) ):
            pending_results.append( pool.apply_async(transcode_with_logging, [f]) )
            # raise exception if any jobs have failed, and prune completed jobs
            completed = [ r for r in pending_results if r.ready() ]
            map(mp.pool.ApplyResult.get, completed)
            pending_results = [ r for r in pending_results if r not in completed ]

        # wait for all remaining results or errors
        while len(pending_results) > 0:
            try:
                # get the results
                pending_results[0].get(timeout=0.1)
                pending_results.pop(0)
            except mp.TimeoutError:
                continue
        if len(pending_results) == 0:
            succeeded = True

    except KeyboardInterrupt:
        terminated = True
        pool.terminate()
        pool.join()
    except Exception as e:
        # catch and log all other exceptions gracefully
        log.exception(e)

    # log our exit status/condition
    overall_time = time.time() - overall_start_time
    if succeeded:
        log.info('Completed transcode in %.2f seconds' % overall_time)
        sys.exit(0)
    elif terminated:
        log.warning('User terminated transcode after %.2f seconds' %
                overall_time)
        sys.exit(3)
    else:
        log.error('Transcode failed after %.2f seconds' % overall_time)
        sys.exit(4)
