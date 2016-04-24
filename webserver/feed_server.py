from generator.generate_feed import PodcastFeedGenerator
from generator.no_such_show_error import NoSuchShowError
from . import settings
from flask import Flask, abort, make_response, redirect, url_for, request
import re
import shortuuid
import sqlite3

app = Flask(__name__)
app.debug = settings.DEBUG


MAX_RECURSION_DEPTH = 20


def find_show(gen: PodcastFeedGenerator, show, strict=True, recursion_depth=0):
    """Get the Show object for the given show_id or show title."""
    if recursion_depth >= MAX_RECURSION_DEPTH:
        raise RuntimeError("Endless loop encountered in SHOW_CUSTOM_URL when searching for {show}.".format(show=show))
    show_id = None
    if not strict:
        # Assuming show is show_id
        try:
            show_id = int(show)
        except ValueError:
            pass
    if not show_id:
        # Assuming show is show name
        try:
            show_id = gen.get_show_id_by_name(show)
        except (KeyError, NoSuchShowError) as e:
            # Perhaps this is an old-style url?
            gen = PodcastFeedGenerator(quiet=True)
            show = show.strip().lower()
            for potential_show, show_id in settings.SHOW_CUSTOM_URL.items():
                potential_show = potential_show.lower()
                if potential_show == show:
                    return find_show(gen, show_id, False, recursion_depth + 1)
            else:
                raise NoSuchShowError from e
    return gen.show_source.shows[show_id]


def url_for_feed(show):
    return url_for("output_feed", show_name=get_feed_slug(show), _external=True)


remove_non_word = re.compile(r"[^\w\d]|_")


def get_feed_slug(show):
    return get_readable_slug_from(show.title)


def get_readable_slug_from(show_name):
    return remove_non_word.sub("", show_name.lower())


@app.before_request
def ignore_get():
    if request.base_url != request.url:
        return redirect(request.base_url, 301)


@app.route('/<show_name>')
def output_feed(show_name):
    gen = PodcastFeedGenerator(quiet=True, pretty_xml=True)  # Make it pretty, so curious people can learn from it
    try:
        show = find_show(gen, show_name)
    except NoSuchShowError:
        abort(404)

    if not show_name == get_feed_slug(show):
        return redirect(url_for_feed(show))

    PodcastFeedGenerator.register_redirect_services(get_redirect_sound, get_redirect_article)

    feed = gen.generate_feed(show.show_id).decode("utf-8")
    # Inject stylesheet processor instruction by replacing the very first line break
    feed = feed.replace("\n",
                        '\n<?xml-stylesheet type="text/xsl" href="' + url_for('static', filename="style.xsl") + '"?>\n',
                        1)
    resp = make_response(feed)
    resp.headers['Content-Type'] = 'application/xml'
    resp.cache_control.max_age = 60*60
    resp.cache_control.public = True
    return resp


# TODO: Create unit tests for the API
@app.route('/api/url/<show>')
def api_url_show(show):
    try:
        return url_for_feed(find_show(PodcastFeedGenerator(quiet=True), show, False))
    except NoSuchShowError:
        abort(404)


@app.route('/api/url/')
def api_url_help():
    return "<pre>Format:\n/api/url/&lt;show&gt;</pre>"


@app.route('/api/slug/')
def api_slug_help():
    return "<pre>Format:\n/api/slug/&lt;show name&gt;</pre>"


@app.route('/api/slug/<show_name>')
def api_slug_name(show_name):
    return url_for('output_feed', show_name=get_readable_slug_from(show_name), _external=True)


@app.route('/api/')
def api_help():
    alternatives = [
        ("Podkast URLs:", "/api/url/"),
        ("Predict URL from show name:", "/api/slug/")
    ]
    return "<pre>API for podcast-feed-gen\nFormat:\n" + \
           ("\n".join(["{0:<20}{1}".format(i[0], i[1]) for i in alternatives])) \
           + "</pre>"


@app.route('/episode/<show>/<episode>')
def redirect_episode(show, episode):
    try:
        return redirect(get_original_sound(find_show(PodcastFeedGenerator(quiet=True), show), episode))
    except ValueError:
        abort(404)


@app.route('/artikkel/<show>/<article>')
def redirect_article(show, article):
    try:
        return redirect(get_original_article(find_show(PodcastFeedGenerator(quiet=True), show), article))
    except ValueError:
        abort(404)

@app.route('/')
def redirect_homepage():
    return redirect(settings.OFFICIAL_WEBSITE)


def get_redirect_db_connection():
    return


def get_original_sound(show, episode):
    with sqlite3.connect(settings.REDIRECT_DB_FILE) as c:
        r = c.execute("SELECT original FROM sound WHERE proxy=?", (episode,))
        row = r.fetchone()
        if not row:
            abort(404)
        else:
            return row[0]

def get_original_article(show, article):
    with sqlite3.connect(settings.REDIRECT_DB_FILE) as c:
        r = c.execute("SELECT original FROM article WHERE proxy=?", (article,))
        row = r.fetchone()
        if not row:
            abort(404)
        else:
            return row[0]


def get_redirect_sound(original_url, episode):
    show = episode.show
    with sqlite3.connect(settings.REDIRECT_DB_FILE) as c:
        try:
            r = c.execute("SELECT proxy FROM sound WHERE original=?", (original_url,))
            row = r.fetchone()
            if not row:
                raise KeyError(episode.sound_url)
            return settings.BASE_URL + url_for("redirect_episode", show=get_feed_slug(show), episode=row[0])
        except KeyError:
            new_uri = shortuuid.uuid()
            e = c.execute("INSERT INTO sound (original, proxy) VALUES (?, ?)", (original_url, new_uri))
            return settings.BASE_URL + url_for("redirect_episode", show=get_feed_slug(show), episode=new_uri)


def get_redirect_article(original_url, episode):
    show = episode.show
    try:
        with sqlite3.connect(settings.REDIRECT_DB_FILE) as c:
            try:
                r = c.execute("SELECT proxy FROM article WHERE original=?", (original_url,))
                row = r.fetchone()
                if not row:
                    raise KeyError(episode.sound_url)
                return settings.BASE_URL + url_for("redirect_article", show=get_feed_slug(show), article=row[0])
            except KeyError:
                new_uri = shortuuid.uuid()
                e = c.execute("INSERT INTO article (original, proxy) VALUES (?, ?)", (original_url, new_uri))
                return settings.BASE_URL + url_for("redirect_article", show=get_feed_slug(show), article=new_uri)
    except sqlite3.IntegrityError:
        # Either the entry was added by someone else between the SELECT and the INSERT, or the uuid was duplicate.
        # Trying again should resolve both issues.
        return get_redirect_article(original_url, episode)


@app.before_first_request
def init_db():
    with sqlite3.connect(settings.REDIRECT_DB_FILE) as c:
        c.execute("CREATE TABLE IF NOT EXISTS sound (original text primary key, proxy text unique)")
        c.execute("CREATE TABLE IF NOT EXISTS article (original text primary key, proxy text unique)")