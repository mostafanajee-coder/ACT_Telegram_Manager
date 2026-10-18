# ACT Telegram Manager — Audit Preview

> **Status: independent security review only. Not approved for production.**
> This source snapshot does not establish that live Telegram integration is safe.

## العربية

### الهدف والبنية

ACT Telegram Manager يقدّم واجهة MCP ضيقة لإعداد المسودات ومراجعتها وإرسالها يدويًا بعد موافقة محلية. يعتمد على مكوّنات Telegram MCP وTelethon، ويضيف سياسة وجهات، وسجلًا محليًا للمسودات، وفصلًا بين إعداد المسودة والموافقة والإرسال.

- **act_manager/policy.py**: إعداد السياسة، قائمة الوجهات، المسودات، منع التكرار، سجل العمليات، وحدود الإعلانات.
- **act_manager/tools.py**: تسجيل أدوات ACT الخمس ومسار التحقق قبل الإرسال.
- **act_server.py** و**telegram_mcp/runner.py**: مدخل MCP الموصى به عبر stdio وحصر سطح الأدوات.
- **act_cli.py**: موافقة المشغّل وإلغاء المسودات من الطرفية المحلية.
- **act_local.py**: تشخيص محلي ومسارات وهمية لا تتصل بتلجرام.
- **tests/**: اختبارات محلية بعملاء وهميين. لا تمثل اختبارات تكامل حي.

### أدوات MCP الخمس

| الأداة | الوظيفة |
| --- | --- |
| **act_policy** | عرض السياسة المحلية دون الاتصال بتلجرام. |
| **act_prepare_post** | إنشاء مسودة محلية. مسار private_user يتحقق من المستلم المحدد ويحوله إلى معرّف User رقمي ثابت. |
| **act_get_post** | عرض حالة مسودة محلية. |
| **act_list_posts** | عرض سجل المسودات المحلية. |
| **act_dispatch_post** | تنفيذ مسودة معتمدة فقط؛ يتحقق من الوجهة ورسالة الرد المحددة قبل الإرسال. |

### سياسة الوجهات

- المجموعة لا تُسمح إلا بإدراجها صراحةً في allowed_chats وبمعرّف مجموعة سالب. العضوية وحدها ليست إذنًا.
- يمكن إعداد مجموعة للردود على رسائل موجودة مع ads_allowed=false. هذا يمنع الإعلانات والمنشورات المستقلة فيها.
- الحد اليومي للإعلانات هو ثلاثة، والفاصل الأدنى بين الإعلانات 60 دقيقة. الحدّان باقيان في محرك السياسة، حتى عندما تكون الإعلانات ممنوعة في الوجهات.
- الرسائل الخاصة لا تُفعّل افتراضيًا ولا تمنح صلاحية دائمة. كل مسودة خاصة تتطلب مستلمًا واحدًا صريحًا ومعرّف رسالة محددًا وموافقة محلية مستقلة على المحتوى والمستلم.
- عند استخدام username، يتحقق النظام من كيان Telegram User ويحفظ المعرّف الرقمي الثابت. يرفض البوتات والحساب نفسه ومعرّفات المجموعات في موضع معرف المستخدم.
- قبل الرد الخاص، يجلب النظام الرسالة المحددة فقط ويتحقق من معرّفها ومرسلها ومحادثتها. لا يقرأ سجل الخاص كاملًا ولا يراقبه ولا يجدول رسائل خاصة.
- لا يوجد إرسال جماعي أو فتح تلقائي لمحادثات تسويقية.

### حد الثقة المعروف

**Known Same-User Trust Boundary Limitation:** بصمة SHA غير موقعة، وقاعدة البيانات المحلية متاحة للعمليات التي تعمل بهوية Windows نفسها. يستطيع برنامج بهذه الهوية تعديل سجل المسودة وإعادة حساب البصمة أو تجاوز واجهة الموافقة المحلية. اختبار Opus 5.5 يعيد إنتاج ذلك عمدًا؛ القيد مقبول لهذا الإصدار المخصص للمراجعة، ولم يُصلح. لا تستخدم هذا المشروع في الإنتاج أو كحاجز أمني بين عمليات Windows ذات الحساب نفسه.

لا يعني نشر الشيفرة اجتياز مراجعة أمنية أو موافقة Telegram. لم تُختبر عملية تسجيل دخول أو قراءة رسالة أو إرسال على حساب حي ضمن هذا الإصدار. أي استخدام فعلي يجب أن يكون بموافقة صاحب الحساب ووفق شروط Telegram وقواعد كل مجموعة.

### إعداد Windows للاختبار المحلي

يتطلب Python 3.11 وuv. من جذر المشروع في PowerShell:

~~~powershell
uv sync --dev --locked
uv run python act_local.py doctor
uv run pytest -q
~~~

تستخدم الاختبارات عملاء وبيانات وهمية؛ لا تشغّل حساب Telegram. ملف act_config.example.json يبدأ بهوية صفرية وقائمة وجهات فارغة، أي وضع منع افتراضي.

لإعادة إنتاج قيد Same-User، شغّل `uv run pytest -q OPUS55_REGRESSION_TESTS/test_p0_approval_boundary.py`. ينجح اختبار التجاوز عندما يعيد إنتاج السلوك المعروف؛ لا تعتبر النتيجة إصلاحًا أمنيًا.

لإعداد اختبار حي لاحقًا باختيارك، انسخ examples/.env.example إلى .env واملأ بيانات API الخاصة بك محليًا، ثم انسخ act_config.example.json إلى act_config.json واضبط الهوية والوجهات التي وافقت عليها. لا تضع أيًا من هذه الملفات في Git. أداة act_login.py تفاعلية وتنشئ ملف جلسة محليًا؛ لم تُشغّل أثناء تجهيز هذا الإصدار.

لا يبدأ خادم MCP إلا عند تشغيله يدويًا. تشغيل uv run python act_server.py يتطلب إعدادًا حيًا وقد يتصل بتلجرام؛ لا تستخدمه لمجرد مراجعة المصدر. يفرض الخادم stdio ويحصر الأدوات في أدوات ACT الخمس.

### الترخيص

يحتفظ المستودع بترخيص Apache 2.0 وإشعار المشروع الأصلي. يتضمن المصدر مكوّنات من [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp)؛ راجع LICENSE قبل إعادة الاستخدام أو التوزيع.

---

## English

### Purpose and architecture

ACT Telegram Manager exposes a narrow MCP surface for preparing drafts, reviewing them, and manually dispatching them after local approval. It builds on Telegram MCP and Telethon components and adds destination policy, a local draft ledger, and separate prepare, approve, and dispatch steps.

- **act_manager/policy.py**: policy configuration, destination allowlist, drafts, deduplication, operation records, and ad limits.
- **act_manager/tools.py**: the five ACT tools and pre-send validation.
- **act_server.py** and **telegram_mcp/runner.py**: the recommended stdio entry point and tool-surface restriction.
- **act_cli.py**: local terminal approval and cancellation.
- **act_local.py**: local diagnostics and mock workflows that do not contact Telegram.
- **tests/**: local tests using fake clients. They are not live integration tests.

### The five MCP tools

| Tool | Function |
| --- | --- |
| **act_policy** | Show local policy without contacting Telegram. |
| **act_prepare_post** | Create a local draft. The private_user path verifies one specified recipient and stores a stable numeric User ID. |
| **act_get_post** | Show a local draft status. |
| **act_list_posts** | List local draft records. |
| **act_dispatch_post** | Dispatch an approved draft only, after validating its destination and specified reply message. |

### Destination policy

- A group is allowed only when explicitly listed in allowed_chats with a negative group ID. Membership alone does not grant permission.
- A group may be configured for replies to existing messages with ads_allowed=false. Ads and standalone posts are then denied.
- The ad limit remains three per day, with at least 60 minutes between ads. Both limits remain in the policy engine even when ads are denied for configured destinations.
- Private messaging is disabled by default and grants no persistent recipient permission. Each private draft requires one explicit recipient, one specific reply-message ID, and separate local approval of that recipient and content.
- A username is resolved to a verified Telegram User; only its stable numeric ID is stored. Bots, the account itself, and group IDs in the User-ID field are rejected.
- Before a private reply, the server fetches only the specified message and checks its ID, sender, and conversation. It does not read full private history, monitor incoming messages, or schedule private messages.
- There is no bulk sending or automatic opening of marketing conversations.

### Known trust boundary

**Known Same-User Trust Boundary Limitation:** the SHA integrity value is unkeyed, and the local database is accessible to processes running as the same Windows user. A process under that identity can alter a draft and recompute the hash or bypass the local approval UI. The Opus 5.5 test intentionally reproduces this behavior. This limitation is accepted for this audit-preview source snapshot and has not been fixed. Do not use this project in production or as a security boundary between processes running under the same Windows account.

Publishing this source does not mean that it passed a security review or that Telegram approved its use. This snapshot has not been tested with a live login, message read, or send. Any real use remains subject to the account owner's approval, Telegram's terms, and each group's rules.

### Windows setup for local tests

Python 3.11 and uv are required. From the project root in PowerShell:

~~~powershell
uv sync --dev --locked
uv run python act_local.py doctor
uv run pytest -q
~~~

Tests use fake clients and dummy data; they do not use a Telegram account. act_config.example.json starts with a zero identity and an empty destination list, so it denies destinations by default.

To reproduce the Same-User limitation, run `uv run pytest -q OPUS55_REGRESSION_TESTS/test_p0_approval_boundary.py`. The exploit test passes when it reproduces the known behavior; that result is not a security fix.

For a later live test that you explicitly choose to perform, copy examples/.env.example to .env and enter your API values locally. Copy act_config.example.json to act_config.json and set the identity and destinations you have approved. Never add those files to Git. act_login.py is interactive and creates a local session file; it was not run while preparing this audit snapshot.

The MCP server starts only when run manually. Running uv run python act_server.py requires live configuration and may connect to Telegram; do not run it just to review the source. The server enforces stdio and limits the exposed surface to the five ACT tools.

### License

The repository retains the Apache 2.0 license and the original project attribution. The source includes components from [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp); review LICENSE before reuse or redistribution.
