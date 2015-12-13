import os
from os import path
import shutil
import tempfile
from urllib.parse import urljoin
from flask import Blueprint, render_template, flash, request, redirect, url_for, abort, jsonify, session
from flask import current_app
from flask.ext.login import login_user, logout_user, login_required

from werkzeug import secure_filename

from cardboardcam.extensions import cache
from cardboardcam.forms import LoginForm, ImageForm
from cardboardcam.models import User

import magic

from basehash import base62
from hexahexacontadecimal import hexahexacontadecimal_encode_int as hh_encode_int
import xxhash

import gc
import base64
from libxmp.utils import file_to_dict
from libxmp import XMPFiles, XMPMeta

main = Blueprint('main', __name__)

# upload_folder = 'uploads'
#
# @main.record
# def record_auth(setup_state):
#     global upload_folder
#     upload_folder = setup_state.app.config.get('UPLOAD_FOLDER')


def upload_dir():
    return current_app.config.get('UPLOAD_FOLDER', '/tmp')


@main.route('/', methods=['GET'])
@cache.cached(timeout=1000)
def home():
    form = ImageForm()
    filename = None
    return render_template('index.html', form=form, filename=filename)


@main.route('/about', methods=['GET'])
@cache.cached(timeout=1000)
def about():
    return render_template('about.html')


@main.route('/upload', methods=['POST'])
def upload():
    # TODO: compare CSRF token from request and session
    # http://flask.pocoo.org/snippets/3/
    # current_app.logger.debug(session['csrf_token'] + ',' + request.headers.get('X-CSRFToken', 'None'))

    file = request.files['file']
    tmp_filename = secure_filename(file.filename)
    tmp_img_path = path.join(upload_dir(), tmp_filename)
    file.save(tmp_img_path)

    # don't accept huge files
    filesize = os.stat(tmp_img_path).st_size
    if filesize > current_app.config.get('MAX_CONTENT_LENGTH', 20*1024*1024):
        return error_page(400, message="Image too large.")  # (400 Bad Request), malformed data from client

    # only accept JPEGs that have EXIF data
    if magic.from_file(tmp_img_path) != b'JPEG image data, EXIF standard':
        return error_page(400, message="No JPEG EXIF data found. Is this really a Cardboard Camera VR image ?")

    hash_id = get_hash_id(tmp_img_path)
    filename = hash_id + '.jpg'
    img_path = path.join(upload_dir(), filename)
    shutil.move(tmp_img_path, img_path)

    try:
        split_vr_image(img_path)
    except Exception as e:
        raise e
        abort(500)

    # return jsonify({'redirect': url_for('main.result', img_filename=filename)})
    return jsonify({'result_fragment': result(img_id=hash_id), 'img_id': hash_id})
    # return redirect(url_for('main.result', img_filename=filename))


def get_hash_id(filepath):
    with open(filepath, 'rb') as file:
        hash_str = base62().encode(xxhash.xxh64(file.read()).intdigest())
        # hash_str = hh_encode_int(xxhash.xxh64(file.read()).intdigest())

    return hash_str


@main.route('/<img_id>', methods=['GET'])
def result(img_id=None):
    img_filename = img_id + '.jpg'
    upload_folder = upload_dir()
    left_img = get_image_name(img_filename, 'left')
    right_img = get_image_name(img_filename, 'right')
    left_img_filepath = path.join(upload_folder, left_img)
    right_img_filepath = path.join(upload_folder, right_img)
    audio_file = path.join(upload_folder, get_audio_file_name(img_filename))
    if path.isfile(left_img_filepath) and path.isfile(right_img_filepath):
        pass
    else:
        abort(404)

    # calculate the thumbnail aspect ratio and height
    from PIL import Image
    image = Image.open(left_img_filepath)
    aspect = float(image.size[1]) / float(image.size[0])  # height / width
    thumb_height = str(int(600 * aspect))

    audio_file_url = None
    if path.isfile(audio_file):
        audio_file_url = url_for('static', filename='uploads/' + get_audio_file_name(img_filename))

    # input_file_url = url_for('static', filename='uploads/' + img_filename)
    # second_image_url = url_for('static', filename='uploads/' + get_second_image_name(img_filename))
    # template = 'result.html'
    template = 'result_fragment.html'
    return render_template(template,
                           # input_img=input_file_url,
                           # second_img=second_image_url,
                           # audio_file_url=audio_file_url,
                           audio_file=audio_file_url,
                           left_img=left_img,
                           right_img=right_img,
                           thumb_height=thumb_height)


def get_image_name(img_filename, eye : str) -> str:
    return path.splitext(img_filename)[0] + "_%s.jpg" % eye


def get_audio_file_name(img_filename):
    return path.splitext(img_filename)[0] + "_audio.mp4"


def join_vr_image(left_img_filename, right_img_filename, audio_filename=None):
    XMP_NS_GPHOTOS_IMAGE = u'http://ns.google.com/photos/1.0/image/'
    XMP_NS_GPHOTOS_AUDIO = u'http://ns.google.com/photos/1.0/audio/'

    tmp_vr_filename = next(tempfile._get_candidate_names())  # tempfile.NamedTemporaryFile().name
    shutil.copy(left_img_filename, tmp_vr_filename)

    # TODO: add extra namespaces found in .vr.jpg files
    # xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"
    # xmlns:xmp = "http://ns.adobe.com/xap/1.0/"
    # xmlns:tiff="http://ns.adobe.com/tiff/1.0/"

    # TODO: if left or right jpg has existing EXIF data, take it (minus the XMP part)
    #       if there is no EXIF data, add some minimal EXIF data
    #       (eg ImageWidth, ImageLength, Orientation, DateTime)

    # TODO: catch XMPError ("bad schema") here
    xmpfile = XMPFiles(file_path=tmp_vr_filename, open_forupdate=True)
    xmp = xmpfile.get_xmp()
    xmp.register_namespace(XMP_NS_GPHOTOS_IMAGE, 'GImage')
    xmp.register_namespace(XMP_NS_GPHOTOS_AUDIO, 'GAudio')

    left_img_b64 = None
    with open(left_img_filename, 'rb') as fh:
        left_img_data = fh.read()
    left_img_b64 = base64.b64encode(left_img_data)
    xmp.set_property(XMP_NS_GPHOTOS_IMAGE, u'GImage:Mime', 'image/jpeg')
    xmp.set_property(XMP_NS_GPHOTOS_IMAGE, u'GImage:Data', left_img_b64.decode('utf-8'))
    del left_img_b64
    # gc.collect()

    right_img_b64 = None
    with open(right_img_filename, 'rb') as fh:
        right_img_data = fh.read()
    right_img_b64 = base64.b64encode(right_img_data)
    xmp.set_property(XMP_NS_GPHOTOS_IMAGE, u'GImage:Mime', 'image/jpeg')
    xmp.set_property(XMP_NS_GPHOTOS_IMAGE, u'GImage:Data', right_img_b64.decode('utf-8'))
    del right_img_b64
    # gc.collect()

    if audio_filename is not None:
        audio_b64 = None
        with open(audio_filename, 'rb') as fh:
            audio_data = fh.read()
        audio_b64 = base64.b64encode(audio_data)
        xmp.set_property(XMP_NS_GPHOTOS_AUDIO, u'GAudio:Mime', 'audio/mp4a-latm')
        xmp.set_property(XMP_NS_GPHOTOS_AUDIO, u'GAudio:Data', audio_b64.decode('utf-8'))
        del audio_b64
        # gc.collect()

    if xmpfile.can_put_xmp(xmp):
        xmpfile.put_xmp(xmp)
    xmpfile.close_file()

    vr_filepath = path.join(upload_dir(), get_hash_id(tmp_vr_filename) + '.vr.jpg')
    shutil.move(tmp_vr_filename, vr_filepath)

    return vr_filepath


def split_vr_image(img_filename):
    XMP_NS_GPHOTOS_IMAGE = u'http://ns.google.com/photos/1.0/image/'
    XMP_NS_GPHOTOS_AUDIO = u'http://ns.google.com/photos/1.0/audio/'

    # TODO: catch XMPError ("bad schema") here
    xmpfile = XMPFiles(file_path=img_filename, open_forupdate=True)
    xmp = xmpfile.get_xmp()

    right_image_b64, right_img_filename = None, None
    audio_b64, audio_filename = None, None

    if xmp.does_property_exist(XMP_NS_GPHOTOS_IMAGE, u'GImage:Data'):
        right_image_b64 = xmp.get_property(XMP_NS_GPHOTOS_IMAGE, u'GImage:Data')
        xmp.delete_property(XMP_NS_GPHOTOS_IMAGE, u'GImage:Mime')
        xmp.delete_property(XMP_NS_GPHOTOS_IMAGE, u'GImage:Data')

    if xmp.does_property_exist(XMP_NS_GPHOTOS_AUDIO, u'GAudio:Data'):
        audio_b64 = xmp.get_property(XMP_NS_GPHOTOS_AUDIO, u'GAudio:Data')
        xmp.delete_property(XMP_NS_GPHOTOS_AUDIO, u'GAudio:Mime')
        xmp.delete_property(XMP_NS_GPHOTOS_AUDIO, u'GAudio:Data')

    # save stripped XMP header to original image
    if xmpfile.can_put_xmp(xmp):
        xmpfile.put_xmp(xmp)
    xmpfile.close_file()

    if right_image_b64:
        # save the right image
        right_img_filename = get_image_name(img_filename, 'right')
        with open(right_img_filename, 'wb') as fh:
            fh.write(base64.b64decode(right_image_b64))
        del right_image_b64
        # gc.collect()

        # add stripped XMP header to the right image
        xmpfile = XMPFiles(file_path=right_img_filename, open_forupdate=True)
        if xmpfile.can_put_xmp(xmp):
            xmpfile.put_xmp(xmp)
        xmpfile.close_file()

    del xmp
    # gc.collect()

    if audio_b64:
        # save the audio
        audio_filename = get_audio_file_name(img_filename)
        with open(audio_filename, 'wb') as fh:
            fh.write(base64.b64decode(audio_b64))
        del audio_b64
        # gc.collect()

    # rename original image
    left_img_filename = get_image_name(img_filename, 'left')
    shutil.move(img_filename, left_img_filename)

    return left_img_filename, right_img_filename, audio_filename


@main.errorhandler(404)
def status_page_not_found(e):
    """
    Renders the error page shown when we call abort(404).
    :type e: int
    :return:
    """
    return error_page(404, message="Not found.")


@main.app_errorhandler(500)
def status_internal_server_error(e):
    """
    Renders the error page shown when we call abort(500).
    :type e: int
    :return:
    """
    return error_page(500, message="Something went wrong.")


def error_page(status_code: int, message=''):
    """
    Renders a custom error page with the given HTTP status code in the response.
    :type status_code: int
    :type message: str
    :return:
    """
    return render_template('error_page_fragment.html', status_code=status_code, message=message), status_code


@main.context_processor
def inject_google_analytics_code():
    """
    This makes the variable 'google_analytics_tracking_id' available for every template to use.
    :rtype: dict
    """
    tracking_id = current_app.config.get('GOOGLE_ANALYTICS_TRACKING_ID', None)
    return dict(google_analytics_tracking_id=tracking_id)


@main.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()

    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).one()
        login_user(user)

        flash('Logged in successfully.', 'success')
        return redirect(request.args.get('next') or url_for('.home'))

    return render_template('login.html', form=form)


@main.route('/logout')
def logout():
    logout_user()
    flash('You have been logged out.', 'success')

    return redirect(url_for('.home'))


@main.route('/restricted')
@login_required
def restricted():
    return 'You can only see this if you are logged in!', 200
