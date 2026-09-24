# exfat-video-recovery

Free, read-only recovery of deleted camera videos from exFAT memory cards (SDXC, CFexpress),
including the **Sony "header-last" layout** that forward-reading carvers can't rebuild.

[العربية ↓](#العربية)

It was built to recover a deleted one-hour 4K clip from a Sony A7S III card. A paid recovery app
listed it as a 47.88 GB `MOV_100396564480.mov` with no preview. Forward-reading carvers such as
PhotoRec can't rebuild this layout. This tool recovered the clip complete. All 91,464 frames were
checked, and a full decode ran with zero errors.

## Why carvers fail on some cameras

Most tools find a video's `ftyp` header and read forward. The Sony A7S III (and likely related
bodies) writes a clip like this:

```
cluster:  S ................................ M ....... H
          [ mdat payload (the video) ......][ moov   ][ header: ftyp · uuid · free · mdat size ]
file:     1                                   ...        0
```

The header cluster comes **after** the data. Reading forward from the header gives you empty
space or other clips. The number in a carved name like `MOV_100396564480.mov` is simply the
header's byte offset in the partition, and the "size" is the mdat length the header declares.

`exfatrec` finds video headers that have no live file: orphans from `map`, or deleted entries
from `scan`. For each one it tries every layout it knows:

- `header-last`: the Sony layout above.
- `header-first`: most cameras.
- `fat-chain`: a leftover FAT chain.

A layout is marked **verified** only when two checks pass:

1. The `moov` index sits exactly where the header's declared size says it should.
2. Sample positions taken from that index land on real frames. The tool checks H.264/HEVC NAL
   length prefixes and Sony `rtmd` signatures.

Guesses are never copied silently.

## Safety

- The card is opened with `O_RDONLY` only. Nothing is ever written to it.
- `extract`, `scan --out` and `map --out` refuse to write onto the card itself. `extract` also
  refuses to overwrite a file. It won't start without enough free space, or on a card other than
  the one the plan was made from (volume serial check). It writes to `name.part` and renames the
  file only once the copy is complete.
- `plan` refuses unverified or partly overwritten layouts unless you add `--force`.
- Before you start, **stop using the card**: don't record on it, and don't copy anything onto it.
  Unmount it; raw reads still work.

## Requirements

- Python 3.8+ (standard library only), on macOS or Linux (tested on macOS).
- `sudo` to read a raw card device. An image file works without it.
- `ffmpeg` / `ffprobe`, for `verify` only.
- Free space on another disk: the size of the video plus about 256 MB.

## Quick start (macOS)

```bash
# 0. Files deleted in Finder are often still in <card>/.Trashes/501 — check there first.
diskutil list                                  # find the card, e.g. disk4
diskutil unmount /dev/disk4s1                  # stop macOS writing to it
mkdir -p ~/exfatrec-work && cd ~/exfatrec-work # keep outputs off the card

# 1. What was deleted? (deleted entries, how much of each is overwritten, and whether its layout is verified)
sudo python3 /path/to/exfatrec.py scan /dev/rdisk4 --out scan.json

# 2. Find orphan video headers (reads the whole card: about 25 min for 128 GB at 90 MB/s)
sudo python3 /path/to/exfatrec.py map /dev/rdisk4 --out map.json

# 3. Make a plan. Use the "header at cluster N" number printed by `map`, or a path printed by `scan`:
python3 /path/to/exfatrec.py plan map.json --header 765839 --out plan.json
#   or: python3 /path/to/exfatrec.py plan scan.json --entry /PRIVATE/M4ROOT/CLIP/C0100.MP4 --out plan.json

# 4. Copy it to ANOTHER disk (it trims the cluster slack itself)
sudo python3 /path/to/exfatrec.py extract /dev/rdisk4 plan.json ~/Movies/recovered.MP4

# 5. Check every frame boundary, then decode every frame
python3 /path/to/exfatrec.py verify ~/Movies/recovered.MP4 --decode
```

Carved name from another tool? Explain it with
`sudo python3 exfatrec.py scan /dev/rdisk4 --offset 100396564480`.

On Linux, use the card device (`/dev/sdX` or `/dev/mmcblkN`) and `umount`; the tool's steps are the
same (this path has not been tested on a Linux machine yet).
`map --first-cluster/--last-cluster` limits the scan to part of the card.

## Commands

| command | what it does |
|---|---|
| `scan`    | Lists live and deleted directory entries. For each deleted file it gives the runs to copy, the method (`contiguous`, `FAT chain intact`, or a layout verified against the moov when the chain is lost), and how much of it is now overwritten. It also flags files still in the card's Trash. `--offset N` walks the MP4 boxes at a byte offset. |
| `map`     | Classifies every cluster (zero, header, moov, Sony metadata, data), with allocation and owner. It lists orphan headers with each matching layout's status: `RECOVERABLE` (verified and intact), overwritten, or frames failed. |
| `plan`    | Turns an orphan header (`--header`) or a deleted entry (`--entry`, plus `--entry-cluster` when two share a path) into cluster runs. `--layout` picks `header-last`, `header-first` or `fat-chain`. `--force` accepts unverified or overwritten layouts. `--force --layout header-first --max-bytes N` copies an unfinalized recording (never from a Sony header-last header). |
| `extract` | Copies the runs into a new file on another disk. It then cuts the file to the directory entry's size, or trims the slack after the last MP4 box. |
| `trim`    | Trims an MP4 by hand. `--dry-run` shows what would be cut. |
| `verify`  | Walks the H.264/HEVC NAL units of every indexed frame and checks every Sony `rtmd` sample. That checks frame boundaries, not picture content. `--decode` also runs a full software decode of every video and audio stream, with error detection. A file with no video stream fails. Exit codes: 0 for OK, 1 for failed, 2 for incomplete (a video stream the structural check can't read; rerun with `--decode`). |

`tools/avcheck.swift` opens a file with AVFoundation, the engine QuickTime and Final Cut use.
Build it with `swiftc -O -o avcheck tools/avcheck.swift && ./avcheck file.MP4`.

## Notes and limits

- Tested on a real Sony A7S III card (XAVC S 4K H.264, exFAT, 128 KiB clusters) and on generated
  exFAT images.
- Other cameras that write the header first are handled by the `header-first` layout.
- Supports exFAT only. FAT32 (cards of 32 GB and smaller) and NTFS are detected and refused.
- Fragmented files are recovered when the FAT chain is intact or a known layout verifies.
  Otherwise the layout shows as `failed` or `unverified`, and nothing is copied without `--force`.
- `map` saying *mdat size is 0* means the recording never finalized (battery pulled, card
  removed). Copy it with `plan --layout header-first --force --max-bytes N`, then rebuild the
  index with [untrunc](https://github.com/anthwlock/untrunc) and a healthy clip from the same
  camera.
- A Sony header-last header whose `moov` is gone had its index overwritten. `plan` refuses to copy
  forward from it, because the video sits *before* that header and copying forward would give you
  other data.
- Don't verify with `ffmpeg -hwaccel videotoolbox`. On macOS it rejects some camera 4K H.264
  streams even from healthy clips. Use `verify --decode` (software) or `avcheck`.
- The volume must use 512-byte or 4096-byte sectors. For anything unusual, give
  `--part-offset` (start LBA × sector size).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The unit tests run anywhere. The end-to-end tests run on macOS and need `hdiutil` and ffmpeg with
libx264. They build a real exFAT image with an MBR holding:

- a deleted clip;
- a deleted Sony-layout clip whose FAT chain is gone;
- two orphan clips (one Sony layout, one header-first).

Each clip must come back byte-for-byte. The tests also check the overwrite, wrong-card, on-card
and free-space refusals.

## License

MIT. See [LICENSE](LICENSE).

---

<div dir="rtl">

## العربية

أداة مجانية لاسترجاع مقاطع الفيديو المحذوفة من بطاقات الذاكرة بنظام exFAT (SDXC وCFexpress). تقرأ البطاقة فقط، ولا تكتب عليها أي شيء.
وتدعم **ترتيب سوني الذي يأتي فيه الرأس بعد البيانات**، وهو ترتيب لا تستطيع أدوات الاستخراج المعتادة إعادة بنائه.

**القصة:** حُذف مقطع مدته ساعة بدقة 4K من بطاقة كاميرا Sony A7S III. برنامج استرجاع مدفوع أظهره كملف بحجم 47.88GB دون معاينة، والأدوات التي تقرأ من الرأس إلى الأمام، مثل PhotoRec، لا تستطيع إعادة بناء هذا الترتيب. هذه الأداة استرجعته كاملاً: فُحصت الإطارات الـ91,464 كلها، وشُغِّل الفيديو كاملاً دون أي خطأ.

### لماذا تفشل الأدوات الأخرى؟

الكاميرا تكتب بيانات الفيديو أولاً، ثم الفهرس (`moov`)، ثم **الرأس (`ftyp`) في النهاية**. الأدوات الأخرى تجد الرأس ثم تقرأ ما بعده، فلا تجد الفيديو.

أما هذه الأداة فتجرّب كل ترتيب تعرفه. ولا تعتبر الترتيب **مؤكَّداً** إلا إذا تحقق شرطان:

1. أن يكون الفهرس في المكان الذي يحدده حجم الرأس بالضبط.
2. أن تقع مواضع الإطارات المسجَّلة في الفهرس على إطارات حقيقية.

ولا تنسخ أي تخمين دون إذنك.

### المتطلبات

- Python 3.8 أو أحدث، على macOS أو Linux. جُرِّبت على macOS.
- `sudo` لقراءة البطاقة.
- ffmpeg، وهو مطلوب لأمر `verify` فقط.
- مساحة فارغة على قرص آخر: حجم الفيديو وزيادة نحو 256MB.

### الأمان

- تُفتح البطاقة للقراءة فقط.
- ترفض الأداة الحفظ على البطاقة نفسها، وترفض استبدال ملف موجود.
- ترفض النسخ إذا لم تكفِ المساحة، أو إذا لم تكن البطاقة هي نفسها التي أُعدّت لها الخطة.
- لا تعتمد ترتيباً غير مؤكَّد أو فوقه بيانات أحدث إلا بالخيار `--force`.
- **توقف عن استخدام البطاقة فوراً:** لا تصوّر عليها ولا تنسخ إليها أي شيء.

### الخطوات (macOS)

<div dir="ltr">

```bash
diskutil list                                   # اعرف رقم البطاقة، مثلاً disk4
diskutil unmount /dev/disk4s1                   # افصلها حتى لا يكتب عليها النظام
mkdir -p ~/exfatrec-work && cd ~/exfatrec-work  # مجلد عمل خارج البطاقة
sudo python3 /path/to/exfatrec.py scan /dev/rdisk4 --out scan.json      # الملفات المحذوفة
sudo python3 /path/to/exfatrec.py map  /dev/rdisk4 --out map.json       # الرؤوس اليتيمة
python3 /path/to/exfatrec.py plan map.json --header <رقم_الكلستر> --out plan.json
#   أو لملف محذوف ظهر في scan:
#   python3 /path/to/exfatrec.py plan scan.json --entry /PRIVATE/M4ROOT/CLIP/C0100.MP4 --out plan.json
sudo python3 /path/to/exfatrec.py extract /dev/rdisk4 plan.json ~/Movies/recovered.MP4
python3 /path/to/exfatrec.py verify ~/Movies/recovered.MP4 --decode
```

</div>

**نصائح:**

- رقم الكلستر هو الرقم الذي يظهر بعد عبارة `header at cluster` في نتيجة أمر `map`. ويطبع الأمر كذلك سطر `next:` جاهزاً للنسخ.
- الملفات التي حذفتها من Finder تبقى غالباً في المجلد المخفي `.Trashes/501` على البطاقة. ابحث عنها هناك أولاً.
- لا تفحص الفيديو باستخدام `-hwaccel videotoolbox` في ffmpeg، لأنه يرفض بعض مقاطع 4K السليمة. استخدم `verify --decode` بدلاً منه.
- إذا قال أمر `map` إن حجم mdat صفر (`mdat size is 0`)، فالتسجيل لم يُغلق بشكل سليم، كأن تكون البطارية فرغت أو أُخرجت البطاقة أثناء التصوير. انسخه بالأمر `plan --layout header-first --force --max-bytes <الحجم>`، ثم أصلحه بأداة untrunc مع مقطع سليم من الكاميرا نفسها.
- إذا كان الرأس بترتيب سوني ولم يُعثر على الفهرس، فقد كُتب فوق الفهرس. هنا ترفض الأداة النسخ إلى الأمام من الرأس، لأن الفيديو يقع قبل الرأس، والنسخ إلى الأمام يعطيك بيانات أخرى.
- بطاقات FAT32 (سعة 32GB فأقل) غير مدعومة.

الرخصة: MIT.

</div>
