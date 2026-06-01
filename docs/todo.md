1.dispatch_agent(agent, prompt, context, files): 把子任务派给一个 spoke。参数设计的不优雅，files目前只针对wiki_curator这一个agent存在
保留通用 files，但描述改成纯机制，把 wiki 专属用法挪进 catalog。 原则：tool 描述只讲机制，per-agent 的用法归 catalog（catalog 本来就是逐 agent 的说明面）。
```
# tool 描述
- files：可选。本次 dispatch 提供给该 agent 的工作区文件（reports//uploads/）。
          是否用、怎么用由各 agent 自定，见下方清单。

# catalog 里 wiki_curator 那条
- wiki_curator：把文档归档进本地 wiki。待归档文件通过 dispatch 的 files 传入，
                prompt 只写归档意图（如何归类/命名）。
```
