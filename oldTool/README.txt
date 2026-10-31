Here's a copy-paste-ready message:

Photo date-stamper script
I've attached Set-PhotoDateFromFilename.ps1. It reads the YYYYMMDD date that's already at the front of our photo filenames and writes that date into the photo's actual metadata, so Explorer/Photos/Google Photos will finally sort them chronologically instead of by whenever the files got copied.

What it does to each photo
• Reads the date prefix from the filename — e.g. 20020128 Oma-01.JPG → January 28, 2002. It also understands 2002-01-28 and 2002_01_28, plus an optional time like 20020128_134500.
• Writes that date into the EXIF "Date taken" field — the one every photo app sorts by.
• Sets the file's Created and Modified timestamps to match.
• Photos with no time in the filename get 12:00 noon.

Important: it never renames your files, and it never re-compresses the image. The picture data comes out byte-for-byte identical — it only adds the date metadata. Files that didn't previously have any metadata will grow by about 1.4 KB, which is just the new metadata block.
How to run it

1. Save the script somewhere easy, like your Desktop.
2. Open PowerShell (Start menu → type "PowerShell").
3. Paste this, adjusting both paths, and press Enter:
powershell -ExecutionPolicy Bypass -File "C:\Users\RAG\Desktop\Image Date Changer\Set-PhotoDateFromFilename.ps1" -Path "D:\Photos\MyFolder" -DryRun

is a preview: it shows you exactly what it would do and changes absolutely nothing. Always do this first. When the preview looks right, run the same command again with -DryRun removed and it'll apply the changes.
You don't need any extra software — this uses features already built into Windows.

Useful options
• -Recurse — also process every subfolder
• -Time 09:30 — use a different time of day instead of noon
• -UseFileTime — keep whatever time of day is already on the file, and only correct the date
• -CsvReport dates.csv — save the report to a spreadsheet
• -SkipMetadata — only fix the Created/Modified timestamps, leave EXIF alone

The report
When it finishes it prints a table of every photo, the date it interpreted, and whether the write succeeded. Anything it couldn't figure out is listed separately at the bottom under "Files needing attention" — those files are left completely untouched, so you can go fix or rename them by hand. It flags both filenames with no date prefix and impossible dates (like 20020230, since February 30th doesn't exist).
Two things to watch out for
1. If the photos are in OneDrive, make sure they're actually downloaded to the PC first — right-click the folder → "Always keep on this device." Cloud-only placeholder files can't be written to.
2. Test on a copy first. Duplicate a folder of ~20 photos and run it there before you point it at the whole collection.

One note for you: since you're sending this to someone else, attach the .ps1 file itself — PowerShell scripts pasted into chat/email often get mangled or blocked.