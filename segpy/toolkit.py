from array import array
from collections import namedtuple, OrderedDict
import itertools
from itertools import zip_longest
import os
import struct
import re

from segpy import textual_reel_header_definition

from segpy.catalog import CatalogBuilder
from segpy.datatypes import CTYPES, size_in_bytes
from segpy.encoding import guess_encoding, is_supported_encoding, UnsupportedEncodingError
from segpy.binary_reel_header_definition import HEADER_DEF
from segpy.ibm_float import ibm2ieee, ieee2ibm
from segpy.revisions import canonicalize_revision
from segpy.trace_header_definition import TRACE_HEADER_DEF
from segpy.util import file_length, batched, pad, round_up, complementary_slices
from segpy.portability import EMPTY_BYTE_STRING

HEADER_NEWLINE = '\r\n'

CARD_LENGTH = 80
CARDS_PER_HEADER = 40

TEXTUAL_HEADER_NUM_BYTES = CARD_LENGTH * CARDS_PER_HEADER
BINARY_HEADER_NUM_BYTES = 400
REEL_HEADER_NUM_BYTES = TEXTUAL_HEADER_NUM_BYTES + BINARY_HEADER_NUM_BYTES
TRACE_HEADER_NUM_BYTES = 240

END_TEXT_STANZA = "((SEG: EndText))"


def extract_revision(binary_reel_header):
    """Obtain the SEG Y revision from the reel header.

    Args:
        binary_reel_header: A dictionary containing a reel header, such as obtained
            from read_binary_reel_header()

    Returns:
        One of the constants revisions.SEGY_REVISION_0 or
        revisions.SEGY_REVISION_1
    """
    raw_revision = binary_reel_header['SegyFormatRevisionNumber']
    return canonicalize_revision(raw_revision)


def num_extended_textual_headers(binary_reel_header):
    """Obtain the number of 3200 byte extended textual file headers.

    A value of zero indicates there are no Extended Textual File Header records
    (i.e. this file has no Extended Textual File Header(s)). A value of -1 indicates
    that there are a variable number of Extended Textual File Header records and the
    end of the Extended Textual File Header is denoted by an ((SEG: EndText)) stanza
    in the final record. A positive value indicates that there are exactly that many
    Extended Textual File Header records.
    """
    num_ext_headers = binary_reel_header['NumberOfExtTextualHeaders']
    return num_ext_headers


def bytes_per_sample(binary_reel_header, revision):
    """Determine the number of bytes per sample from the reel header.

    Args:
        binary_reel_header: A dictionary containing a reel header, such as obtained
            from read_binary_reel_header()

        revision: One of the constants revisions.SEGY_REVISION_0 or
            revisions.SEGY_REVISION_1

    Returns:
        An integer number of bytes per sample.
    """
    dsf = binary_reel_header['DataSampleFormat']
    bps = HEADER_DEF["DataSampleFormat"]["bps"][revision][dsf]
    return bps


def samples_per_trace(binary_reel_header):
    """Determine the number of samples per trace_samples from the reel header.

    Note: There is no requirement for all traces to be of the same length,
        so this value should be considered indicative only, and as such is
        mostly useful in the absence of other information. The actual number
        of samples for a specific trace_samples should be retrieved from individual
        trace_samples headers.

    Args:
        binary_reel_header: A dictionary containing a reel header, such as obtained
            from read_binary_reel_header()

    Returns:
        An integer number of samples per trace_samples
    """
    return binary_reel_header['ns']


def trace_length_bytes(binary_reel_header, bps):
    """Determine the trace_samples length in bytes from the reel header.

    Note: There is no requirement for all traces to be of the same length,
        so this value should be considered indicative only, and as such is
        mostly useful in the absence of other information. The actual number
        of samples for a specific trace_samples should be retrieved from individual
        trace_samples headers.

    Args:
        binary_reel_header:  A dictionary containing a reel header, such as obtained
            from read_binary_reel_header()

        bps: The number of bytes per sample, such as obtained from a call to
            bytes_per_sample()

    """
    return samples_per_trace(binary_reel_header) * bps + TRACE_HEADER_NUM_BYTES


def read_textual_reel_header(fh, encoding=None):
    """Read the SEG Y card image header, also known as the textual header

    Args:
        fh: A file-like object open in binary mode positioned such that the
            beginning of the textual header will be the next byte to read.

        encoding: Optional encoding of the header in the file. If None (the
            default) a reliable heuristic will be used to guess the encoding.
            Either 'cp037' for EBCDIC or 'ascii' for ASCII.

    Returns:
        A tuple of forty Unicode strings (Python 2: unicode, Python 3: str)
        containing the transcoded header data.
    """
    raw_header = fh.read(TEXTUAL_HEADER_NUM_BYTES)

    num_bytes_read = len(raw_header)
    if num_bytes_read < TEXTUAL_HEADER_NUM_BYTES:
        raise EOFError("Only {} bytes of {} byte textual reel header could be read"
                       .format(num_bytes_read, TEXTUAL_HEADER_NUM_BYTES))

    if encoding is None:
        encoding = guess_encoding(raw_header)

    lines = tuple(bytes(raw_line).decode(encoding) for raw_line in batched(raw_header, CARD_LENGTH))
    return lines


def read_binary_reel_header(fh, endian='>'):
    """Read the SEG Y binary reel header.

    Args:
        fh: A file-like object open in binary mode. Binary header is assumed to
            be at an offset of 3200 bytes from the beginning of the file.

        endian: '>' for big-endian data (the standard and default), '<' for
            little-endian (non-standard)
    """
    reel_header = {}
    for key in HEADER_DEF:
        pos = HEADER_DEF[key]['pos']
        ctype = HEADER_DEF[key]['type']
        values = tuple(read_binary_values(fh, pos, ctype, 1, endian))
        reel_header[key] = values[0]
    return reel_header


def has_end_text_stanza(ext_header):
    """Determine whether the header is the end text stanza.

    Args:
        ext_header: A sequence of forty CARD_LENGTH character Unicode strings.

    Returns:
        True if the header is the SEG Y Revision 1 end text header,
        otherwise False.
    """
    return END_TEXT_STANZA in ext_header[0]


def read_extended_headers_until_end(fh, encoding):
    """Read an unspecified number of extended textual headers, until the end-text header is found.

    Args:
        fh: A file-like object open in binary mode. The first of any extended textual headers
            is assumed to be at an offset of 3600 bytes from the beginning of the file
            (immediately following the binary reel header).

        encoding: Optional encoding of the header in the file. If None (the
            default) a reliable heuristic will be used to guess the encoding.
            Typically 'cp037' for EBCDIC or 'ascii' for ASCII.

    Returns:
        A list of tuples each containing forty CARD_LENGTH character Unicode strings. If present, the end_text
        stanza is excluded.
    """
    extended_headers = []
    while True:
        ext_header = read_textual_reel_header(fh, encoding)
        extended_headers.append(ext_header)
        if has_end_text_stanza(ext_header):
            break
    return extended_headers


def read_extended_headers_counted(fh, num_expected, encoding):
    """Read a specified number of extended textual headers.

    If an end-text stanza is located prematurely (in anything other than the last expected header)
    reading will be terminated and a warning logged.

    Args:
        fh: A file-like object open in binary mode. The first of any extended textual headers
            is assumed to be at an offset of 3600 bytes from the beginning of the file
            (immediately following the binary reel header).

        num_expected: A non-negative integer of headers.

        encoding: Optional encoding of the header in the file. If None (the
            default) a reliable heuristic will be used to guess the encoding.
            Typically 'cp037' for EBCDIC or 'ascii' for ASCII.

    Returns:
        A list of tuples each containing forty CARD_LENGTH -character Unicode strings.
    """
    assert num_expected >= 0
    extended_headers = []
    for i in range(num_expected):
        ext_header = read_textual_reel_header(fh, encoding)
        if has_end_text_stanza(ext_header):
            if i != num_expected - 1:
                print("Unexpected end-text extended header") # TODO: Log this
            break
        extended_headers.append(ext_header)

    return extended_headers


def read_extended_textual_headers(fh, binary_reel_header, encoding):
    """Read any extended textual reel headers.

    Args:
        fh: A file-like object open in binary mode. The first of any extended textual headers
            is assumed to be at an offset of 3600 bytes from the beginning of the file
            (immediately following the binary reel header).

        binary_reel_header: A dictionary containing data read from the binary
            reel header by the read_binary_reel_header() function.

        encoding: Optional encoding of the header in the file. If None (the
            default) a reliable heuristic will be used to guess the encoding.
            Typically 'cp037' for EBCDIC or 'ascii' for ASCII.

    Returns:
        A sequence of sequences of Unicode strings representing headers of lines of characters. The length of the
        outer sequence will be equal to the number of extended headers read. Each item in the outer sequence will be
        a sequence of exactly forty Unicode strings.  To combine the headers into a single string, consider using
        concatenate_extended_textual_headers().

    Postcondition:
        As a post-condition to this function, the file-pointer of fh will be
        positioned immediately after the last extended textual header, which
        should be the start of the first trace_samples header.
    """
    fh.seek(REEL_HEADER_NUM_BYTES)
    declared_num_ext_headers = num_extended_textual_headers(binary_reel_header)

    extended_headers = []
    if declared_num_ext_headers < 0:
        return read_extended_headers_until_end(fh, encoding)

    return read_extended_headers_counted(fh, declared_num_ext_headers, encoding)


_READ_PROPORTION = 0.75  # The proportion of time spent in catalog_traces
                         # reading the file. Determined empirically.


def catalog_traces(fh, bps, endian='>', progress=None):
    """Build catalogs to facilitate random access to trace_samples data.

    Note:
        This function can take significant time to run, proportional
        to the number of traces in the SEG Y file.

    Four catalogs will be build:

     1. A catalog mapping trace_samples index (0-based) to the position of that
        trace_samples header in the file.

     2. A catalog mapping trace_samples index (0-based) to the number of
        samples in that trace_samples.

     3. A catalog mapping CDP number to the trace_samples index.

     4. A catalog mapping an (inline, crossline) number 2-tuple to
        trace_samples index.

    Args:
        fh: A file-like-object open in binary mode, positioned at the
            start of the first trace_samples header.

        bps: The number of bytes per sample, such as obtained by a call
            to bytes_per_sample()

        endian: '>' for big-endian data (the standard and default), '<'
            for little-endian (non-standard)

        progress: A unary callable which will be passed a number
            between zero and one indicating the progress made. If
            provided, this callback will be invoked at least once with
            an argument equal to 1

    Returns:
        A 4-tuple of the form (trace_samples-offset-catalog,
                               trace_samples-length-catalog,
                               cdp-catalog,
                               line-catalog)` where
        each catalog is an instance of ``collections.Mapping`` or None
        if no catalog could be built.
    """
    progress_callback = progress if progress is not None else lambda p: None

    if not callable(progress_callback):
        raise TypeError("catalog_traces(): progress callback must be callable")

    trace_header_format = compile_trace_header_format(endian)

    length = file_length(fh)

    pos_begin = fh.tell()

    trace_offset_catalog_builder = CatalogBuilder()
    trace_length_catalog_builder = CatalogBuilder()
    line_catalog_builder = CatalogBuilder()
    alt_line_catalog_builder = CatalogBuilder()
    cdp_catalog_builder = CatalogBuilder()

    for trace_number in itertools.count():
        progress_callback(_READ_PROPORTION * pos_begin / length)
        fh.seek(pos_begin)
        data = fh.read(TRACE_HEADER_NUM_BYTES)
        if len(data) < TRACE_HEADER_NUM_BYTES:
            break
        trace_header = TraceHeader._make(trace_header_format.unpack(data))
        num_samples = trace_header.ns
        trace_length_catalog_builder.add(trace_number, num_samples)
        samples_bytes = num_samples * bps
        trace_offset_catalog_builder.add(trace_number, pos_begin)
        # Should we check the data actually exists?
        line_catalog_builder.add((trace_header.Inline3D,
                                  trace_header.Crossline3D),
                                 trace_number)
        alt_line_catalog_builder.add((trace_header.TraceSequenceFile,
                                     trace_header.cdp),
                                     trace_number)
        cdp_catalog_builder.add(trace_header.cdp, trace_number)
        pos_end = pos_begin + TRACE_HEADER_NUM_BYTES + samples_bytes
        pos_begin = pos_end

    progress_callback(_READ_PROPORTION)

    trace_offset_catalog = trace_offset_catalog_builder.create()
    progress_callback(_READ_PROPORTION + (_READ_PROPORTION / 4))

    trace_length_catalog = trace_length_catalog_builder.create()
    progress_callback(_READ_PROPORTION + (_READ_PROPORTION / 2))

    cdp_catalog = cdp_catalog_builder.create()
    progress_callback(_READ_PROPORTION + (_READ_PROPORTION * 3 / 4))

    line_catalog = line_catalog_builder.create()

    if line_catalog is None:
        # Some 3D files put Inline and Crossline numbers in (TraceSequenceFile, cdp) pair
        line_catalog = alt_line_catalog_builder.create()

    progress_callback(1)

    return (trace_offset_catalog,
            trace_length_catalog,
            cdp_catalog,
            line_catalog)


def read_trace_header(fh, trace_header_format, pos=None):
    """Read a trace_samples header.

    Args:
        fh: A file-like-object open in binary mode.

        trace_header_format: A Struct object, such as obtained from a
            call to compile_trace_header_format()

        pos: The file offset in bytes from the beginning from which the data
            is to be read.

    Returns:
        A TraceHeader object.
    """
    if pos is not None:
        fh.seek(pos)
    data = fh.read(TRACE_HEADER_NUM_BYTES)
    trace_header = TraceHeader._make(
        trace_header_format.unpack(data))
    return trace_header


def read_binary_values(fh, pos=None, ctype='l', count=1, endian='>'):
    """Read a series of values from a binary file.

    Args:
        fh: A file-like-object open in binary mode.

c

        ctype: The SEG Y data type.

        number: The number of items to be read.
    Returns:
        A sequence containing count items.
    """
    fmt = CTYPES[ctype]
    item_size = size_in_bytes(fmt)
    block_size = item_size * count

    fh.seek(pos, os.SEEK_SET)
    buf = fh.read(block_size)

    if len(buf) < block_size:
        raise EOFError("{} bytes requested but only {} available".format(
            block_size, len(buf)))

    values = (unpack_ibm_floats(buf, count)
              if fmt == 'ibm'
              else unpack_values(buf, count, item_size, fmt, endian))
    assert len(values) == count
    return values


def unpack_ibm_floats(data, count):
    """Unpack a series of binary-encoded big-endian single-precision IBM floats.

    Args:
        data: A sequence of bytes. (Python 2 - a str object,
            Python 3 - a bytes object)

        count: The number of floats to be read.

    Returns:
        A sequence of floats.
    """
    return array('f', (ibm2ieee(data[i: i+4]) for i in range(0, count * 4, 4)))


def unpack_values(buf, count, item_size, fmt, endian='>'):
    """Unpack a series items from a byte string.

    Args:
        data: A sequence of bytes. (Python 2 - a str object,
            Python 3 - a bytes object)

        count: The number of floats to be read.

        fmt: A format code (one of the values in the datatype.CTYPES
            dictionary)

        endian: '>' for big-endian data (the standard and default), '<'
            for little-endian (non-standard)

    Returns:
        A sequence of objects with type corresponding to the format code.
    """
    c_format = '{}{}{}'.format(endian, count, fmt)
    return struct.unpack(c_format, buf)
    # We could use array.fromfile() here. On the one hand it's likely
    # to be faster and more compact,
    #
    # On the other, it only works on "real" files, not arbitrary
    # file-like-objects and it would require us to handle endian byte
    # swapping ourselves.


def format_standard_textual_header(revision, **kwargs):
    """Produce a standard SEG Y textual header.

    Args:
        revision: The SEG Y revision.

        **kwargs: Named arguments corresponding to the values in the
            textual_reel_header_definition.TEMPLATE_FIELD_NAMES dictionary,
            which in turn correspond to the placeholders in the
            textual_reel_header_definition.TEMPLATE string.  Any omitted
            arguments will result in placeholders being replaced by spaces.
            If the end_marker argument is not supplied, an appropriate end
            marker will be selected based on the SEG Y revision. For standard
            end markers consider using textual_reel_header_definition.END_TEXTUAL_HEADER
            or textual_reel_header_definition.END_EBCDIC.

    Returns:
        A list of forty Unicode strings.

    Usage:
        header = format_standard_textual_header(1,
                                            client="Lundin",
                                            company="Western Geco",
                                            crew_number=123,
                                            processing1="Sixty North AS",
                                            sweep_start_hz=10,
                                            sweep_end_hz=1000,
                                            sweep_length_ms=10000,
                                            sweep_channel_number=3,
                                            sweep_type='spread')

    """

    kwargs.setdefault('end_marker', textual_reel_header_definition.END_MARKERS[revision])

    template = textual_reel_header_definition.TEMPLATE

    placeholder_slices = parse_template(template)
    background_slices = complementary_slices(placeholder_slices.values(), 0, len(template))

    chunks = []
    for bg_slice, placeholder in zip_longest(background_slices, placeholder_slices.items()):

        if bg_slice is not None:
            chunks.append(template[bg_slice])

        if placeholder is not None:
            ph_name, ph_slice = placeholder
            ph_arg_name = textual_reel_header_definition.TEMPLATE_FIELD_NAMES[ph_name]
            ph_value = kwargs.pop(ph_arg_name, '')
            ph_len = ph_slice.stop - ph_slice.start
            substitute = str(ph_value)[:ph_len].ljust(ph_len, ' ')
            chunks.append(substitute)

    if len(kwargs) > 0:
        raise TypeError("The following keyword arguments did not correspond to template placeholders: {!r}"
                        .format(list(kwargs.keys())))

    concatenation = ''.join(chunks)
    lines = concatenation.splitlines(keepends=False)

    return lines[1:]  # Omit the first and last lines, which are artifacts of the multiline string template


def parse_template(template):
    """Parse a template to produce a dictionary of placeholders.

    Args:
        template: The template string containing { field-name } style fixed-width fields.

    Returns:
        A OrderedDict mapping field names to slices objects which can be used to index
        into the template string. The order of the entries is the same as the order within
        which they occur in the template.
    """
    PATTERN = r'\{\s*(\w*)\s*\}'
    regex = re.compile(PATTERN)
    matches = regex.finditer(template)

    fields = OrderedDict()
    for match in matches:
        name = match.group(1)
        start = match.start()
        end = match.end()
        fields[name] = slice(start, end)

    return fields


def write_textual_reel_header(fh, lines, encoding):
    """Write the SEG Y card image header, also known as the textual header

    Args:
        fh: A file-like object open in binary mode positioned such that the
            beginning of the textual header will be the next byte to read.

        lines: An iterable series of forty lines, each of which must be a
            Unicode string of CARD_LENGTH characters. The first three characters
            of each line are often "C 1" to "C40" (as required by the SEG Y
            standard) although this is not enforced by this function, since
            many widespread SEG Y readers and writers do not adhere to this
            constraint.  To produce a SEG Y compliant series of header lines
            consider using the format_standard_textual_header() function.

            Any lines longer than CARD_LENGTH characters will be truncated without
            warning.  Any excess lines over CARDS_PER_HEADER will be discarded.  Short
            or omitted lines will be padded with spaces.

        encoding: Typically 'cp037' for EBCDIC or 'ascii' for ASCII.
    """
    # TODO: Seek
    padded_lines = [line.encode(encoding).ljust(CARD_LENGTH, ' '.encode(encoding))[:CARD_LENGTH]
                    for line in pad(lines, padding='', size=CARDS_PER_HEADER)]
    header = ''.join(padded_lines)
    assert len(header) == 3200
    fh.write(header)


def write_binary_reel_header(fh, binary_reel_header, endian='>'):
    """Write the binary_reel_header to the given file-like object.

    Args:
        fh: A file-like object open in binary mode for writing.

        binary_reel_header: A dictionary of values using a subset of the keys
            in binary_reel_header_definition.HEADER_DEF associated with
            compatible values.
    """
    # TODO: Seek
    for key in HEADER_DEF:
        pos = HEADER_DEF[key]['pos']
        ctype = HEADER_DEF[key]['type']
        value = binary_reel_header[key] if key in binary_reel_header else HEADER_DEF[key]['def']
        write_binary_values(fh, [value], ctype, pos)


def format_extended_textual_header(text, encoding, include_text_stop=False):
    """Format a string into pages and line suitable for an extended textual header.

    Args
        text: An arbitrary text string.  Any universal newlines will be preserved.

        encoding: Either ASCII ('ascii') or EBCDIC ('cp037')

        include_text_stop: If True, a text stop stanza header will be appended, otherwise not.
    """

    if not is_supported_encoding(encoding):
        raise UnsupportedEncodingError("Extended textual header", encoding)

     # According to the standard: "The Extended Textual File Header consists of one or more 3200-byte records, each
     # record containing 40 lines of textual card-image text." It goes on "... Each line in an Extented Textual File
     # Header ends in carriage return and linefeed (EBCDIX 0D25 or ASCII 0D0A)."  Given that we're dealing with fixed-
     # length (80 byte) lines, this implies that we have 78 bytes of space into which we can encode the content of each
     # line, which must be left-justified and padded with spaces.

    width = CARD_LENGTH - len(HEADER_NEWLINE)
    original_lines = text.splitlines()

    # Split overly long lines (i.e. > 78) and pad too-short lines with spaces
    lines = []
    for original_line in original_lines:
        padded_lines = (pad_and_terminate_header_line(original_line[i:i+width], width)
                        for i in range(0, len(original_line), width))
        lines.extend(padded_lines)

    pages = list(batched(lines, 40, pad_and_terminate_header_line('', width)))

    if include_text_stop:
        stop_page = format_extended_textual_header(END_TEXT_STANZA, encoding)[0]
        pages.append(stop_page)

    return pages


def pad_and_terminate_header_line(line, width):
    return line.ljust(width, ' ') + HEADER_NEWLINE


def write_extended_textual_headers(fh, pages, encoding):
    """Write extended textual headers.

    Args:
        fh: A file-like object open in binary mode for writing.

        pages: An iterables series of sequences of Unicode strings, where the outer iterable
            represents 3200 byte pages, each comprised of a sequence of exactly 40 strings of nominally 80 characters
            each.  Although Unicode strings are accepted, and when encoded they should result in exact 80 bytes
            sequences.  To produce a valid data structure for pages, consider using format_extended_textual_header()

        encoding: Either 'cp037' for EBCDIC or 'ascii' for ASCII.

    Raises:
        ValueError: If the provided header data has the wrong shape.
        UnicodeError: If the textual data could not be encoded into the specified encoding.

    """
    # TODO: Seek

    if not is_supported_encoding(encoding):
        raise UnsupportedEncodingError("Writing extended textual header", encoding)

    encoded_pages = []
    for page_index, page in enumerate(pages):
        encoded_page = []
        # TODO: Share some of this code with writing the textual reel header.
        for line_index, line in enumerate(page):
            encoded_line = line.encode(encoding)
            num_encoded_bytes = len(encoded_line)
            if num_encoded_bytes != CARD_LENGTH:
                raise ValueError("Extended textual header line {} of page {} at {} bytes is not "
                                 "{} bytes".format(line_index, page_index, num_encoded_bytes, CARD_LENGTH))
            encoded_page.append(encoded_line)
        num_encoded_lines = len(encoded_page)
        if num_encoded_lines != CARDS_PER_HEADER:
            raise ValueError("Extended textual header page {} number of "
                             "lines {} is not {}".format(num_encoded_lines, CARDS_PER_HEADER))
        encoded_pages.append(encoded_page)

    for encoded_page in encoded_pages:
        concatenated_page = b''.join(encoded_page)
        assert(len(concatenated_page) == TEXTUAL_HEADER_NUM_BYTES)
        fh.write(concatenated_page)


def write_trace_header(fh, trace_header, trace_header_format, pos=None):
    """Write a TraceHeader to file.

    Args:
        fh: A file-like object open in binary mode for writing.

        trace_header: A TraceHeader object.

        trace_header_format: A Struct object, such as obtained from a
            call to compile_trace_header_format()

        pos: An optional file offset in bytes from the beginning of the
            file. Defaults to the current file position.
    """
    if pos is not None:
        fh.seek(pos, os.SEEK_SET)
    buf = trace_header_format.pack(*trace_header)
    fh.write(buf)


def write_trace_samples(fh, values, ctype='l', pos=None):
    write_binary_values(fh, values, ctype, pos)


def write_binary_values(fh, values, ctype='l', pos=None):
    """Write a series on values to a file.

    Args:
        fh: A file-like-object open for writing in binary mode.

        values: An iterable series of values.

        ctype: The SEG Y data type.

        pos: An optional offset from the beginning of the file. If omitted,
            any writing is done at the current file position.
    """
    fmt = CTYPES[ctype]

    if pos is not None:
        fh.seek(pos, os.SEEK_SET)

    buf = (pack_ibm_floats(values)
           if fmt == 'ibm'
           else pack_values(values, fmt))

    fh.write(buf)


def pack_ibm_floats(values):
    """Pack floats into binary-encoded big-endian single-precision IBM floats.

    Args:
        values: An iterable series of numeric values.

    Returns:
        A sequence of bytes. (Python 2 - a str object, Python 3 - a bytes
            object)
    """
    return EMPTY_BYTE_STRING.join(ieee2ibm(value) for value in values)


def pack_values(values, fmt, endian='>'):
    """Pack values into binary encoded big-endian byte strings.

    Args:
        values: An iterable series of values.

        fmt: A format code (one of the values in the datatype.CTYPES
            dictionary)

        endian: '>' for big-endian data (the standard and default), '<'
            for little-endian (non-standard)
    """
    c_format = '{}{}{}'.format(endian, len(values), fmt)
    return struct.pack(c_format, *values)


# TODO: Consider generalising the below to also produce a ReelHeader record. Then modify
#       read_binary_reel_header() to return such a record, and write_binary_reel_header() to accept such
#       a record.


_TraceAttributeSpec = namedtuple('Record', ['name', 'pos', 'type'])


def compile_trace_header_format(endian='>'):
    """Compile a format string for use with the struct module from the
    trace_samples header definition.

    Args:
        endian: '>' for big-endian data (the standard and default), '<' for
            little-endian (non-standard)

    Returns:
        A string which can be used with the struct module for parsing
        trace_samples headers.

    """

    record_specs = sorted(
        [_TraceAttributeSpec(name,
                             TRACE_HEADER_DEF[name]['pos'],
                             TRACE_HEADER_DEF[name]['type'])
         for name in TRACE_HEADER_DEF],
        key=lambda r: r.pos)

    fmt = [endian]
    length = 0
    for record_spec in record_specs:

        shortfall = length - record_spec.pos
        if shortfall:
            fmt.append(str(shortfall) + 'x')  # Ignore bytes
            length += shortfall

        ctype = CTYPES[record_spec.type]
        fmt.append(ctype)
        length += size_in_bytes(ctype)

    assert length == TRACE_HEADER_NUM_BYTES

    return struct.Struct(''.join(fmt))


def _compile_trace_header_record():
    """Build a TraceHeader namedtuple from the trace_samples header definition"""
    record_specs = sorted(
        [_TraceAttributeSpec(name,
                             TRACE_HEADER_DEF[name]['pos'],
                             TRACE_HEADER_DEF[name]['type'])
         for name in TRACE_HEADER_DEF],
        key=lambda r: r.pos)
    return namedtuple('TraceHeader',
                      (record_spec.name for record_spec in record_specs))


TraceHeader = _compile_trace_header_record()


if __name__ == '__main__':
    from pprint import pprint as pp
    header = format_standard_textual_header(1,
                                            client="Lundin",
                                            company="Western Geco",
                                            crew_number=123,
                                            processing1="Sixty North AS",
                                            sweep_start_hz=10,
                                            sweep_end_hz=1000,
                                            sweep_length_ms=10000,
                                            sweep_channel_number=3,
                                            sweep_type='spread')
    pp(header, width=200)
