# How to be more ambitious: A tutorial for humans with models
# 如何变得更有雄心：以模型为基础的人类深度思考教程

- **原作者**：Felix Rieseberg（Anthropic Claude 工程主管，负责 Claude.ai、Cowork、Claude Code Desktop）
- **原文发布日期**：2026年10月13日
- **原文链接**：https://felixrieseberg.com/how-to-be-more-ambitious-a-tutorial-for-humans-with-models/

---

## 一、核心背景与四维雄心框架 (Introduction & The Four Dimensions)

> **原文核心摘录**：
> I frequently proclaim (together with other people working on AI) that we ought to be more ambitious when using it. It's become a rallying cry. Whenever we see something impressive, we repost and write "we're not ambitious enough". I understand how tricky it is to follow that advice, so here's a tutorial on being more ambitious.
>
> I think of ambition in four types: **Speed, Altitude, Depth, and Range**.
> - **Speed**: The same work in less time, the bread and butter for AI's economic value: If you need some code written, I'll do it faster with Claude. Well established and requires little creativity in how you approach AI, so I'll skip this one.
> - **Altitude**: Going up in abstraction and considering the outcome rather than the individual task.
> - **Depth**: Considering a task the way a field-leading expert would. Rather than operating at the task's layer, we go one level deeper and touch the underlying data, tools, and problems.
> - **Range**: Drawing on multiple fields rather than just the one the task might need at the surface, using knowledge, tools, and techniques you normally wouldn't.

**中文精要解读**：
在 Claude 团队内部有一句口号：“在使用人工智能时，我们应当更具雄心（We ought to be more ambitious when using AI）。”
雄心可以分为四个维度：**速度（Speed）、高度（Altitude）、深度（Depth）和范围（Range）**。
目前绝大多数人只停留在第一层“速度”（写文章更快、写代码更快），但这连 AI 潜力的 1% 都没开发出来。真正区分高手与小白的鸿沟，在于**高度、深度与范围**。

---

## 二、第一维：高度 (Altitude) —— 向上追问一层，关注目标背后的目标

> **原文核心摘录**：
> We often flippantly call this "letting the model operate at a higher abstraction". When you give the model a task, it's executing your best guess at how to get the outcome you want. To be more ambitious in altitude, ask yourself - **what do you want to happen next? What's the goal beyond the goal?** At the beginning of last year, I still asked Claude to fix individual crashes for me. Eventually I asked Claude to simply keep our apps crash-free.

### 案例：季度业务汇报（Quarterly Business Review, QBR）的三级升维
不要只让 AI 按模板更新数字做几页 PPT，而是逐步提升抽象高度：
1. **Level 1（看清变化、归因与对策）**：
   - *"Tell me what changed this quarter, why, and what we should do about it."*
   - （把你能拿到的所有原始数据都喂给模型：“告诉我本季度发生了什么变化、原因是什么，以及我们应该采取什么应对措施。”）
2. **Level 2（锁定最紧迫决策与正反论据）**：
   - *"Work out which three decisions are most urgent in the face of everything you've learned, and start working on potential directions with for/against arguments."*
   - （“基于你所了解到的一切，梳理出当前最紧迫的三个决策，并附上正反两方面的论据来探讨潜在方向。”）
3. **Level 3（重塑机制本身，建立早期预警）**：
   - *"The review exists so leadership catches problems early and moves money and people to the right places. Design a better way to do that than a quarterly deck, show from our own history that it would have caught things sooner, and run it beside the current one for a quarter."*
   - （“季度复盘存在的意义是让管理层及早发现问题，并把钱和人调配到正确的地方。请设计一种比季度 PPT 更好的机制来达成这一目标，用我们自己的历史数据证明它能更早捕捉到问题，并在本季度与现有机制并行运行。”）

### 持久闭环工作流 (Durable Workflows Taking the Human Out of the Loop)
高度也是人们构建“脱离人工频繁干预的持久工作流”的地方。例如，为了让 Claude 保证应用“零崩溃（crash-free）”，Felix 并不是每次手动复制报错给它，而是改造工作流：让 Claude 能够直接拉取崩溃报告、理解应用版本号与 Git Commit 的映射关系，并赋予它访问 Windows、macOS 和 Linux 测试机的权限，从而独立完成复现与修复验证。

---

## 三、第二维：深度 (Depth) —— 像行业顶尖专家一样直达底层

> **原文核心摘录**：
> Sometimes, you just want a task executed really well. Being ambitious in depth means pushing the model to use the tools, techniques, media, and knowledge employed by the best professionals in the field - and to keep pushing until even those people might learn something new from your work.

### 五大深度实战技法
1. **坚持使用行业顶尖专家所用的专业工具（Insist on the same instruments used by the best in the industry）**：
   - 当你提示模型“像专家一样思考”时，模型往往只是**切换了词汇（switched vocabulary），却没有切换方法（without actually switching methods）**。
   - 当模型试图用 Markdown 表格敷衍你时，强制要求它使用顶尖数据科学家和统计学家真正使用的数据库与统计模型。你甚至不需要自己知道专家用什么工具，直接要求模型列出并使用它们即可。
2. **设定看似“不合理”的高目标，并告诉模型这完全合理可行（Pick unreasonable goals and tell the model that they're very reasonable）**：
   - 想把网站加载速度从 10 秒优化到 5 秒？直接要求优化到 **200 毫秒**。如果模型觉得某件事看似不可能，再次向它重申这件事完全可解（very much solvable），让它继续深挖不要停。
3. **对抗性挑战你的既有认知（Challenge what you know）**：
   - 商业和工程中我们深信不疑的许多“常识”往往只是对现实的粗糙近似。给模型提供最原始的信息和数据（raw information and data），要求它以对抗性视角（adversarially）检验假设。例如：*“我们认为用户流失是新手引导（onboarding）的问题。请试着从原始数据中复现并检验这一假设。”*
4. **生产全新知识与发现盲区（Produce new knowledge）**：
   - 当模型已经深入原始数据内部后，进一步要求它寻找盲区：*“基于我们的原始日志构建一个能够复现过去一年客户行为的仿真模型。然后找出我们尚未察觉的最早期流失信号，并在它未见过的数据集上证明其预测能力。”*
5. **使用外部客观裁判，绝不让模型自评（Use an outside judge）**：
   - **不要让模型自己裁判自己（Do not ask the model to judge itself）！** 必须坚持引入外部验证（outside validation），最理想的是模型可以立即运行并获得即时反馈的工具（如编译器、测试套件、回测脚本、仿真器）。

### 🔥 经典深度重写元指令 (The Deep-Rewrite Meta-Prompt)

**英文原文**：
```text
I want a deeper version of this task, not a bigger one. Before rewriting it: (1) say what a top professional in this field would do here that I'm not asking for; (2) name the rawest material they would start from instead of what has already been digested for me; (3) name the specific methods and tools they would use; (4) say what, other than you or me, would show the result is right. Then rewrite my prompt to ask for exactly that.
```

**中文标准译文**：
```text
我想要的是这个任务更深入（deeper）的版本，而不是一个更大（bigger）的版本。在重写题目之前：
（1）请说明该领域的顶尖专家在这里会做哪些我没有要求的事情；
（2）请列出他们会从哪些最原始的素材入手，而不是我已经整理消化好的素材；
（3）请列出他们会使用的具体方法和工具；
（4）除了你我之外，还有什么可以证明结果是正确的。
然后，请修改我的题目，使其完全符合上述要求。
```

---

## 四、第三维：范围 (Range) —— 名词转动词与跨界特种战队

> **原文核心摘录**：
> In short, being ambitious in range means that we consider our work from the perspective of different fields - more fields than would have been economical in the past. This isn't a particularly unique insight, every single heist movie spends the first 30 minutes assembling a diverse cast of characters and talents.

### 两大跨界落地技法
1. **将指令中的名词替换为动词（Replace the nouns with verbs）**：
   - 源自 IDEO 汤姆·凯利（Tom Kelley）与乔纳森·利特曼（Jonathan Littman）在《创新的艺术（The Art of Innovation）》中的经典技巧。
   - 一份季度汇报不是一个静态名词“PPT（a deck）”，而是一个动词过程：**“在 30 分钟内，让忙碌的决策者相信一段关于因果关系的叙事，并在不确定性下做出资源调配决定（getting busy people to believe a story about cause and effect, and to decide under uncertainty, in 30 minutes）”**。
   - 一旦把名词变成动词，你和 AI 都能立刻看清应该从哪些跨界领域借用方法！
2. **组建跨界特种战队与反方红队（Assemble the crew & Adversarial Red Team）**：
   - 如果这份汇报关乎地球存亡，人类会组建什么样的团队？
     - **制作与洞察组**：数据分析师（归因模型） + 调查记者（挖掘事实与叙事手法） + 顶尖编辑（表达结构） + 动效与音频设计师（呈现体验）。
     - **对抗性反方红队（Adversarial Red Team）**：引入**做空机构研究员（Short seller）**与**法务会计（Forensic accountant）**，专门挑刺与反向审查。
   - ⚠️ **反角色扮演铁律（Anti-Roleplay Rule）**：
     > *"It is easy to accidentally engage in role play, which will make your transcript weirder without improving the actual outcome. Use this method to find fields and techniques. Don't ask the model to write like a short seller, ask it to do what a short seller does to research their position."*
     > （千万不要让 AI 搞表面角色扮演！不要让模型“用做空者的语气写稿”，那只会让文字变得阴阳怪气而不会提升实际结果。要直接要求模型**调用做空者研究头寸的具体方法**，让不同的专业工具各自解决一个真实问题。）

---

## 五、底层心理障壁：突破自我效能感 (Psychological Self-Efficacy)

为什么人类明明拥有强大的模型，却总是提不出有雄心的要求？
Felix 指出，心理学将其称为 **自我效能感（Self-efficacy）**（引用 Locke & Latham 35 年目标设定研究与 Stripe 创始人 Patrick Collison 的 "Fast" 清单）：
1. **相信某件事可能做到，才能做成难事（Accomplish hard things by believing them to be possible）**：1939 年乔治·丹齐格（George Dantzig）上课迟到，把黑板上两道统计学百年未解难题当成课后作业解了出来。
2. **看见别人做到后，迅速突破极限（Accomplish hard things after watching somebody else do them）**：人类追逐“4分钟一英里”数十年未果，1954年5月 Roger Bannister 突破后仅 46 天纪录就再次被破；马拉松破二（Sabastian Sawe 在伦敦跑出首个官方破二成绩，同场亚军也同时破二）亦是如此。
3. **允许自己失败，放下过度紧绷（Accomplish hard things by giving yourself permission to fail）**：正如《网球的内心游戏（The Inner Game of Tennis）》以及 Linus 当年发布 Linux 时所说“只是一个业余爱好”，卸下心理包袱反而能释放最大创造力。
