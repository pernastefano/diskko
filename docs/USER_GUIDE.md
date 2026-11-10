# User guide

DISKKO shows what your music library holds, what is missing, and gets the rest for you. This guide
follows the order you will meet things in. Administrators also see the **Settings**, **Jobs** and
**Status** icons and the **Artist management** page; everyone else sees the rest.

## First run

The first time you open DISKKO it asks you to create the **administrator**: pick a name and a
password (at least 8 characters). After that, the sign-in page is the front door.

1. **Settings → Library**: choose the **Default source**: your music **folder** or **Plex**. Run a scan
   (**Scan library**). It runs as a job; follow it under **Jobs**.
2. **Settings → Integrations**: fill in the **User-Agent** for MusicBrainz (a name/version and an email,
   it is what MusicBrainz asks for), then **Plex** (address and token) and **SLSKD** (address and API
   key) if you use them. Last.fm (a free API key) adds similar artists and biographies.
3. **Settings → Users**: add the people who will use it. Everybody has their own account.

The **Status** icon in the top bar turns green when the services you configured answer, yellow when
only some do and red when none does. Click it to test them.

## The library

**Dashboard** is the home page: how many artists, albums and tracks you have, albums by quality (each
opens the matching albums) and the last scan; administrators also get the source, folder, schedule and
scan buttons.

**Artists** shows every artist as a card. The badge reads **owned / total albums**:

| Badge | Meaning |
|-------|---------|
| green `10 / 10` | You have all of the artist's albums. |
| yellow `4 / 7` | You have some and miss others. |
| red `0 / 5` | You have none of them (the artist is in your library through something else, such as a compilation). |
| *no data yet* | The artist has not been matched to MusicBrainz yet. |

"Total" counts the albums of the **types** chosen in Settings → Albums (albums and EPs by default),
so a compilation or a live album you own does not count as a studio album. Search as you type, jump with
the A–Z bar, tick **Only with missing albums**, sort by name, missing or recently added, and switch
between grid and list.

**Albums** lists every album you own, with filters for audio **quality**, format, type and
**metadata**: albums not matched to MusicBrainz, matches to be checked, albums with **fewer or
more tracks** than the MusicBrainz release (often just the wrong release, fixed by matching
again), without a year, without a cover or whose files carry no MusicBrainz ids. The track
counts come from MusicBrainz: an administrator reads them with **Check against MusicBrainz**
(a job, about one album per second); until then *Track count not checked yet* lists the albums
that were not compared. The search box also finds an album by its year.

**An artist's page** has the picture, a short biography (Wikipedia in your language, Last.fm if
Wikipedia has nothing), the **top 10 tracks** with 30-second previews, the albums you own and the
ones you miss, grouped by type. Biography and tracks are fetched in the background the first time
you open it.

**An album's page** shows the track list with the quality of each file. If you own the album you
can play it in the browser (FLAC, MP3, AAC/M4A, OGG/Opus, WAV, AIFF) with the **player** stuck to
the bottom of the page: play/pause, previous, next, a seek bar, volume, **shuffle** and **repeat**
(off, all tracks, this track). Each track has a play button that starts that track in the player
and an **info** button (the *i* icon) with everything readable from the file: size, type, format,
bitrate and its mode, sample rate, bit depth, channels, duration, encoder, ReplayGain, embedded cover
and all the tags. The player lives on the album page: going to another page stops it. If the album
is not owned, tracks have a 30-second preview from iTunes or Deezer when one exists. Administrators can **refresh the metadata**
or open **Manage matching**: a page for that album with three parts.

- **The album and its current match**, with **Compare with the current match**: title, year and
  number of tracks, then every track side by side (yours with the file, MusicBrainz's with its
  position and length), marking the tracks that differ, the files MusicBrainz does not list and the
  tracks you have no file for. Also *Not on MusicBrainz* and *Back to automatic*.
- **Artist**, for an album filed under the wrong artist (a namesake, or a similar name). Write the
  artist's name in the one field and **Search**: MusicBrainz is searched and every result is listed
  (with the artists you already have), plus **Create a new artist** with the name you wrote. Pick
  one and press **Move the album here**; nothing changes before that, and **Cancel** puts the
  original artist back in the field. The album is matched again under its new artist, and every
  later scan keeps it there whatever the tags or Plex say.
- **Album**: write the album's name and the release groups MusicBrainz finds are listed (in the
  artist's releases or in all of MusicBrainz; by title, by any words, or with your own MusicBrainz
  query). Albums that only a fallback provider knows are listed too. **Show versions** reads the
  releases (editions) of a group, which can have different numbers of tracks, and puts each next to
  your files: how many files have a track there, then track by track. **Use this release** keeps your
  choice for good and, with *update tags and file names* ticked, rewrites the tags and the file names
  from that release (a job, like **Update tags** below). **Use the album only** keeps the choice
  without touching the files.

### Updating the tags of an album

Administrators can re-tag an album that is already in the library: **Update tags** on the album
page (only for albums matched to MusicBrainz with certainty, or chosen by hand). It works in two steps:

1. **Preview the changes** reads the files and the MusicBrainz release and shows, file by file,
   the tags now and after (click a file to see them), the tracks of the release that have **no
   file** in your library (a 10-file album against an 11-track release lists the missing track),
   files that match no track (they are left alone) and what would happen to the cover. Nothing is
   written. When several releases fit (the standard and the deluxe edition), pick the one to use
   and preview again.
2. **Apply these changes** writes the tags. Every file is written as a copy that is checked and
   then replaces the original, so an error never leaves a half-written file.

Only the tags DISKKO manages (title, artists, album, track and disc numbers, date, release type,
MusicBrainz ids) are changed: genre, lyrics, comments, ReplayGain and an existing embedded cover
stay. Files without a cover get one from the Cover Art Archive (or the album folder, or Deezer);
*Replace the embedded cover* does it for every file. *Also rename the files and the folder* (library
read from the music folder only) applies the naming templates of the import; a name that is already
taken is skipped, and a folder shared with other albums is not renamed. Plex is told to scan the
album afterwards. Tracks that are missing are never created: download the album again for them.

### Quality

Every file gets the best **quality profile** it satisfies and an album is as good as its worst
track. The standard profiles are:

| Profile | Requirement |
|---------|-------------|
| FLAC Master | Lossless, 24-bit, 192 kHz or more |
| FLAC Hi-Res | Lossless, 24-bit, 96 kHz or more |
| FLAC Standard | Lossless, 16-bit, 44.1 kHz or more (the default) |
| MP3 320 / V0 | 320 kbps, V0, or lossless |
| MP3 ≥ 256, MP3 ≥ 192 | At least that bitrate, or lossless |
| Any | Everything (the seed profile may still be called *Qualsiasi*; rename it in Settings → Quality) |

Administrators can edit, add and reorder profiles under **Settings → Quality**. DISKKO reads the
file's real parameters; it does not analyze the audio, so an MP3 re-encoded to FLAC still counts as FLAC.

## Requesting an album

On an artist's page, click **Request download** on a missing album (or **Request upgrade** on the
page of an album you own, to get a better quality). The request appears under **Requests** and goes through:

1. **Queued**, then **Searching** SLSKD.
2. **Found**: a source must look like the album (name, number of tracks) and reach the minimum
   quality. In automatic mode the best-scored one is downloaded; in manual mode the request stops on
   **Choose one** and you pick a candidate with **Download this**.
3. **Downloading**, with a progress bar, then **Downloaded**. A source that goes offline, stalls or keeps you queued for
   hours is skipped for the next candidate.
4. **Processing** and **Importing**: files are checked, tagged from MusicBrainz, given cover art, named from the
   template and moved into your library in one step. Plex is told to scan that folder (the whole music section if it has to).
5. **In library**: the album is no longer missing. With Plex as the library source the album is added to DISKKO as soon as Plex has it
   (no need to scan Plex again).

When the files do not match a MusicBrainz release on their own (a different edition, odd file
names, a missing track), nothing is thrown away: the request shows **Choose one** and its page lists
the releases of the album with how many files match each. Pick the release to tag from, **Import
with the tags of the files**, or **Do not import** (the download then goes to the quarantine).
Files named after a vinyl side (`A1 - Title`) are recognised by the side number MusicBrainz gives.

**Match by hand** (on the request page) shows the files next to the tracks of a release, with the
pairing the engine would make: change the track of any file, leave a file out (it keeps its title),
pick another release of the album, or, when MusicBrainz gave the wrong album, search MusicBrainz for
another one and use it (the request then follows that album). Nothing is imported until you press
**Import with this matching**.

Whenever a request waits for you, in manual mode or because the files do not match, you get a
notification in the bell. When you choose a source to download, **Files** shows what the folder holds
(names, sizes, bitrates, lengths) and **Show all** the folders beyond the first few; **Search the
network by hand** looks with your own words, like SLSKD itself, and lists every folder with audio.

If something else cannot be imported (a damaged file, too low a quality), the request shows
**Failed** with the reason and the files wait in a quarantine folder. Administrators open it from
the **Quarantine** link on the Requests page: **Match and import** takes a folder back and asks how to
match it with MusicBrainz (the quality is not asked again), and **Delete selected** removes the
folders that cannot be rescued.
When the album's folder already exists, files with the same names are overwritten (a file that
belongs to another album is never touched). When an **upgrade** replaces an album, the old files, their
cover and the folders left empty are deleted once the new copy is in place; set `KEEP_REPLACED=true`
to move the old files to `.diskko-replaced` in your music folder instead. If an album is in your
library more than once, its page shows the copies (administrators): keep one and the others are deleted.

### The Requests page

Everyone sees their own requests, administrators everyone's (and can filter by user). Filter by
**In progress**, **Completed** or **Failed or cancelled**. Each row offers **Cancel request** while
it runs, **Search again** after a failure (with another search text if you like), and **Delete**
once it is finished (the downloaded files and your library are not touched).

### Notifications

The bell in the top bar counts what you have not read. You are told, once per request and kind of event, when a request
of yours **completes**, **fails**, **finds nothing** or **waits for your choice**. Click it for a menu with the latest five (each links to its request,
and opening one marks it read), **Mark all as read**, or **See all notifications** for the latest 50.
It updates by itself every 30 seconds, when you come back to the tab and right after anything you do.
Read ones disappear after 30 days. Requests that were already finished
before notifications existed do not produce one.

## Discovering music

**Search** looks first in your library (artists, albums by title, artist or year), then asks
MusicBrainz for **artists** and for **albums**, even ones you do not own. Album results are the
albums whose title or artist contains your words; **Open the artist** goes to their discography,
where you can request the album. For anything else use MusicBrainz fields: `tag:dance` or
`tag:rock` finds albums by genre tag, `releasegroup:abbey AND artist:beatles` is an exact query.
DISKKO itself does not store genres, so the library part cannot search by genre. **Open** creates a page with
their discography, so you can request albums. Artists you open but never add are forgotten after
90 days. Artist pages also show **similar artists** (Last.fm, or ListenBrainz when Last.fm is not
set up), with an *In library* badge on those you have. Album pages show **similar albums**: up to
two albums (the ones you own first, then the newest) of each similar artist DISKKO already knows
(open a similar artist to add its albums to the list). No other lookup is made.

## The mobile version

Open `/m/` (or **Mobile version** in the footer) on a phone for a simplified DISKKO with four
tabs: **Artists**, **Albums**, **Search** and **Requests**.

- An artist shows the top 10 tracks with their preview, the albums you own, the ones you miss
  (with **Request download**) and similar artists.
- An album shows its tracks: the ones you own play from your library in a player stuck above
  the tabs (play/pause, previous, next, seek, shuffle, repeat) that goes through the whole album
  and keeps playing with the screen locked; the others have the 30-second preview.
- **Search** looks in your library and on MusicBrainz for artists you do not have.
- **Requests** follows each request: progress, cancel, search again, delete, and, when a request
  waits for your choice, the list of sources with **Download this**.

**Preferences** (menu under your name) has the language, the theme (Light, Dark or Automatic),
the password change and the credits with the link to DISKKO's home page.

Jobs, status, settings, matching, re-tagging and the other administration stay on the desktop
site (**Desktop version** in the menu under your name).

It is a PWA: **Add to Home Screen** installs it as an app with its own icon. Browsers only
install it as a full app, and only run its service worker (the offline page and faster start),
over **HTTPS** (or `localhost`); over plain `http://` on the local network the pages still work
and the home-screen shortcut opens them without the browser bars on iPhone, but Chrome on
Android offers only a shortcut. Put DISKKO behind an HTTPS reverse proxy to get the full app.

## Your preferences

Open your name in the top bar → **Preferences**: language (English or Italian), theme (**Light** is
Sand, **Dark** is Graphite, or **Automatic** to follow your device) and the **minimum quality** your
own requests aim for (or the system default). Change your password from the same menu. The album
types counted as "missing" are the same for everyone and set by an administrator.

## For administrators

- **Settings → Library**: source, folder (with *Check folder*), scan schedule, **Scan library** and **Full rescan**.
  The source you choose is the one that counts: after a scan, rows left by the other source are
  removed so every album is listed once. A scan that would drop more than half the known tracks stops
  (usually an unmounted folder) and changes nothing.
- **Settings → Integrations / Services**: connection details, and for each capability (metadata,
  covers, previews, similar artists, top tracks, biographies) a default service and fallbacks. A
  service that fails five times in a row is paused for ten minutes; the others take over.
- **Settings → Library → Import**: the folder and file name templates for imported albums, and the
  quarantine folder for downloads that cannot be imported.
- **Settings → Jobs**: how many jobs run at once and how long finished ones and their logs are kept.
- **Jobs** (top bar): live progress and logs, with cancel and retry; buttons filter by status
  (with the number of jobs in each) on top of the type and log search. Yellow means a job is running (or about to),
  red means one failed and you have not looked yet: opening the job list turns it off, and it comes
  back only for new failures. With nothing running the icon is plain.
- **Artist management**: the review queue for artists MusicBrainz could not match with certainty
  (choose the right one, paste an MBID, or mark *not on MusicBrainz*). When MusicBrainz does not have
  an artist or album, DISKKO may use Deezer if it is certain; those artists are marked *Data from
  Deezer, to be checked* and can be kept, retried on MusicBrainz or corrected.
- **Settings → Audit log**: who did what and when (requests, sign-ins, accounts, settings, decisions).
  It records names, never passwords or secrets, and no IP addresses; entries are kept for a year.
- **Settings → Backups**: a copy of the database is made every night and the latest seven are kept.
  **Back up now** makes one on demand and each copy can be downloaded. Keep a copy off the machine.
  A backup contains your saved tokens, so store it like the database itself.
- **Sign-in protection**: five wrong passwords for one account within five minutes pauses that
  account for a few minutes, even for the right password. The attempt appears in the audit log.

## Troubleshooting

| Symptom | What to do |
|---------|------------|
| An artist badge says *no data yet* | Its MusicBrainz sync has not run. Check **Jobs**, and that the MusicBrainz contact is set in Settings → Integrations. |
| An artist is matched to the wrong namesake | **Artist management** → the artist → **Manage matching**, pick the right one or paste its MBID. |
| "Too many failed attempts" at sign-in | Wait a few minutes; an administrator can also reset your password. |
| A request says *No results* | The page lists the texts tried (the artist and album, the album alone, the artist's last name with the album, and the last name alone, which asks for many answers and keeps only the folders that are the album). Search again with a shorter text of your own (the artist's last name, or only the album title), or choose manually (set the mode to manual in Settings → Integrations). |
| A request failed and files are in quarantine | Open the request: the reason and the folder are shown. Fix the cause (or accept a lower quality) and **Search again**. |
| Plex does not show the new album | Check Plex under **Status**; if Plex sees the files at another path, set the path map in Settings → Integrations → Plex. |
| The status icon is red | Click it, then **Check all now**: the failing service shows what went wrong. |
| I forgot my password | Ask an administrator (**Settings → Users → reset password**). There is no email recovery. |
