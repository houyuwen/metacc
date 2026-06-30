# metacc: 用 libclang 给 C 项目做一层轻量静态元编程

项目地址：[houyuwen/metacc.git](https://github.com/houyuwen/metacc.git)

很多 C 项目都会在规模变大以后遇到同一个问题：模块越来越多，注册点越来越分散，但运行时并不想引入复杂的动态注册机制。

比如一个嵌入式平台里可能有这些需求：

- 各个模块在自己的 `.c` 文件里声明初始化函数，最终按优先级组成一张启动表。
- 不同板级文件各自贡献设备 provider，平台层按优先级统一遍历。
- 事件总线的订阅者分散在测试或业务模块中，构建时自动收拢成静态数组。
- 数据中心的 slot 分布在多个翻译单元里，但运行时只想访问一张只读表。

最直接的做法是手写一个全局数组。但这会把所有模块重新耦合到一个中心文件里：每新增一个模块，都要去修改那张数组。另一种做法是运行时注册，但在 MCU、RTOS 或偏底层 SDK 里，这通常意味着额外的初始化顺序、锁、链表、堆内存或不可控的启动路径。

`metacc` 选择第三条路：在源码里放非常薄的标记宏，构建时用 `libclang` 扫描所有编译单元，把分散的条目生成普通 C 源码。

最后得到的是普通的：

```c
extern const my_entry_t my_table[];
extern const uint32_t my_table_count;
```

没有运行时注册，没有堆内存，没有链接脚本段，也没有编译器私有 section 约定。它只是把“人工维护表”的动作交给构建期工具来做。

## 核心模型

`metacc` 的核心模型很小：声明一张生成物，再从多个编译单元向它追加条目。

它主要由四个标记宏组成：

```c
METACC_TABLE(type, name, ...)
METACC_TABLE_ITEM(name, ...)
METACC_ENUM(type, ...)
METACC_ENUM_ITEM(type, ...)
```

其中：

- `METACC_TABLE` 声明一张表，通常放在拥有该表类型定义的头文件里。
- `METACC_TABLE_ITEM` 向表里追加一个条目，通常分散写在各个 `.c` 或测试 `.cpp` 翻译单元里。
- `METACC_ENUM` 声明一个生成枚举类型。
- `METACC_ENUM_ITEM` 向枚举追加一个枚举常量。
- 工具会为每张表生成一个 `metacc_<owner>.h` 和 `metacc_<owner>.c`。
- 生成的 `.c` 会包含表声明所在的 owner header，以及对应的 generated header。
- 每张表生成两个符号：`const <type> <name>[]` 和 `const uint32_t <name>_count`。
- 每个枚举生成一个 `typedef enum { ... } <type>;`，枚举项按常量名字符串排序，不显式赋值。

为了让生成结果稳定、可读，业务侧推荐直接使用这些原生标记宏。条目里引用的回调函数或对象保持外部可链接，生成文件就能像普通业务代码一样引用它们。

## 四个典型示例

### 示例一: 构建期枚举

假设项目希望多个模块分散声明内部事件 ID，最终生成一份稳定排序的枚举定义。

先在 owner header 里声明枚举类型：

```c
#pragma once

#include "metacc.h"

METACC_ENUM(eventbus_event_t, count = EVENTBUS_EVENT_COUNT)
```

然后任意 `.c` 文件都可以贡献枚举项：

```c
#include "eventbus_events.h"

METACC_ENUM_ITEM(eventbus_event_t, EVENTBUS_EVENT_BETA)
METACC_ENUM_ITEM(eventbus_event_t, EVENTBUS_EVENT_ALPHA)
```

构建后，`metacc` 会在 companion header 中生成：

```c
typedef enum {
    EVENTBUS_EVENT_ALPHA,
    EVENTBUS_EVENT_BETA,
    EVENTBUS_EVENT_COUNT,
} eventbus_event_t;
```

注意：默认枚举值由 C 编译器按顺序自增。按常量名排序只能保证生成结果可重复，不适合协议号、持久化 key 等跨版本必须稳定的外部 ID。

### 示例二: 模块初始化表

假设项目希望各个模块在自己的源文件里声明初始化入口，最终由构建系统生成一张按优先级排序的启动表。

先在头文件里定义条目类型，并声明表：

```c
#pragma once

#include <stdint.h>
#include "metacc.h"

typedef struct {
    uint8_t level;
    const char *name;
    int (*init)(void);
    void (*exit)(void);
} module_init_entry_t;

METACC_TABLE(module_init_entry_t, module_init, sort_col = 0, order = asc)
```

然后任意 `.c` 文件都可以贡献条目：

```c
#include "module_init.h"

int device_init(void);
void device_exit(void);

METACC_TABLE_ITEM(
    module_init,
    20,
    "device",
    device_init,
    device_exit
)
```

构建后，`metacc` 会生成类似这样的 C 代码：

```c
#include "../../../include/module_init.h"
#include "../include/metacc_module_init.h"

int device_init(void);
void device_exit(void);

const module_init_entry_t module_init[] = {
    {20, "device", device_init, device_exit},
};
const uint32_t module_init_count = 1u;
```

对业务代码来说，这就是一张普通的只读数组：

```c
for (uint32_t i = 0; i < module_init_count; ++i) {
    module_init[i].init();
}
```

### 示例三: 设备树 provider 表

另一个常见场景是设备树或板级设备表。每个板级文件静态定义自己的设备实例，再提供一个 provider 函数按索引返回设备。平台层只关心最终生成的 provider 表，不需要知道这些 provider 分散在哪些源文件里。

表声明可以这样写：

```c
typedef const struct device *(*plat_device_provider_t)(size_t index);

typedef struct {
    uint8_t priority;
    plat_device_provider_t provider;
} plat_devicetree_provider_entry_t;

METACC_TABLE(plat_devicetree_provider_entry_t, device_tree, sort_col = 0, order = asc)
```

某个板级文件贡献自己的 UART provider：

```c
const struct device *board_uart_provider(size_t index);

METACC_TABLE_ITEM(
    device_tree,
    PLAT_DEVICETREE_PRIO_UART,
    board_uart_provider
)
```

构建后生成的 `device_tree[]` 会按 `priority` 排序。平台层可以基于这张表实现设备查找、初始化和反初始化：查找时遍历 provider 匹配设备名，初始化时按优先级顺序执行，释放时按反向顺序执行。

这个例子里，`metacc` 解决的是板级解耦问题。新增一个设备 provider 时，不需要修改中心数组；provider 写在对应板级文件里，构建期自动进入 `device_tree[]`。

### 示例四: 事件总线订阅表

事件总线适合拓扑相对固定的内部事件，例如链路状态变化、传感器数据到达、模块健康状态更新。每个模块在自己的源文件里声明订阅关系，构建期生成最终订阅表。

订阅者结构可以这样定义：

```c
typedef struct eventbus_subscriber {
    eventbus_event_t event;
    void (*cb)(const void *data, size_t len, void *user_data);
    void *user_data;
} eventbus_subscriber_t;

METACC_TABLE(eventbus_subscriber_t, eventbus_subscribers, sort_col = 0, order = asc)
```

任意模块可以在自己的翻译单元里追加订阅者：

```c
void alpha_cb(const void *data, size_t len, void *user_data);
void beta_cb(const void *data, size_t len, void *user_data);

METACC_TABLE_ITEM(eventbus_subscribers, EVENTBUS_EVENT_ALPHA, alpha_cb, NULL)
METACC_TABLE_ITEM(eventbus_subscribers, EVENTBUS_EVENT_BETA, beta_cb, NULL)
```

生成结果是一张按 `event` 排序的 `eventbus_subscribers[]`。`eventbus_publish()` 先用 lower bound 找到某个事件的第一个订阅者，再顺序通知同一事件下的所有回调。查找成本是 `O(log N)`，通知成本是 `O(K)`，其中 `K` 是该事件的订阅者数量。

这个例子里，`metacc` 解决的是事件拓扑维护问题。发布者不需要知道订阅者在哪个文件里，订阅者也不需要调用运行时注册接口；构建期生成的静态表就是最终分发拓扑。

## 宏参数语义

### METACC_TABLE

```c
METACC_TABLE(item_type, table_name, sort_col = 0, order = asc)
```

前两个参数是必需的。

- `table_name`：生成的数组符号名。
- `item_type`：数组元素类型。

可选参数：

- `sort_col` 或 `col`：按第几个 payload 字段排序，使用从 0 开始的索引。
- `order`：`asc` 或 `desc`，默认 `asc`。

如果没有 `sort_col`，条目会按源码路径和行号排序。这适合不关心优先级、只想稳定输出的表。

如果设置了 `sort_col`，工具会尝试把该字段解析为整数或枚举常量，再排序。它支持常见 C 整数字面量后缀，比如 `1u`、`0x10UL`。如果字段不是数字也不是已知枚举，会退回字符串排序。

### METACC_TABLE_ITEM

```c
METACC_TABLE_ITEM(table_name, field0, field1, field2, ...)
```

第一个参数是目标表名，后面的 payload 会成为数组元素初始化内容。

如果 payload 只有一个顶层参数，生成时不会额外套花括号：

```c
METACC_TABLE_ITEM(callbacks, on_event)
```

生成：

```c
const callback_t callbacks[] = {
    on_event,
};
```

如果 payload 有多个顶层参数，会生成聚合初始化：

```c
METACC_TABLE_ITEM(module_init, 20, "device", device_init, device_exit)
```

生成：

```c
const module_init_entry_t module_init[] = {
    {20, "device", device_init, device_exit},
};
```

### METACC_ENUM

```c
METACC_ENUM(enum_type, count = ENUM_COUNT)
```

第一个参数是生成的枚举类型名，必须是 C 标识符。

可选参数：

- `count`：可选的末尾计数枚举项名，例如 `EVENTBUS_EVENT_COUNT`。不传就不生成 count 项。

### METACC_ENUM_ITEM

```c
METACC_ENUM_ITEM(enum_type, ENUM_ITEM_NAME)
```

第一个参数是目标枚举类型名，第二个参数是枚举常量名。所有枚举常量会按常量名字符串排序输出，生成器不写显式数值：

```c
typedef enum {
    ENUM_ITEM_ALPHA,
    ENUM_ITEM_BETA,
} enum_type;
```

## 为什么不用运行时注册

运行时注册在桌面程序里很常见，但在嵌入式或底层 SDK 中往往带来额外复杂度。

注册函数需要被调用，就会引入初始化顺序问题。注册容器需要存储，就会引入数组容量、链表、堆内存或锁。注册发生在运行时，也意味着错误发现更晚：某个模块忘了调用注册函数，可能要到系统启动后才暴露。

`metacc` 把这件事提前到构建期。

源码里只留下声明：

```c
METACC_TABLE_ITEM(...)
```

构建时把它们收集起来：

```c
const entry_t table[] = {
    ...
};
```

运行时只读数组即可。没有注册动作，也没有注册失败路径。

## 为什么用 libclang，而不是正则扫源码

`metacc` 会做一层快速文本预筛，但真正提取宏实例依赖 `libclang`。

原因很简单：C 项目里的源码形态并不单纯。一个文件可能通过 `-I` 引入头文件，头文件还会再 include 其它头文件。宏参数里可能有函数指针、字符串、聚合初始化、括号表达式。单纯正则很容易在这些地方出错。

当前实现的处理方式是：

1. 从 `compile_commands.json` 读取每个编译单元的真实编译参数。
2. 根据 `-I`、`-iquote`、`-isystem` 等参数做快速 include 预扫，跳过完全不含 `METACC_*` 的文件。
3. 对可能含有标记的编译单元调用 `libclang`，读取宏实例和 enum 值。
4. 只收集 `project_root` 内的注解，并排除已生成目录，避免外部依赖或旧生成物污染结果。
5. 将结果按表名归并、排序、去重、生成 C/H 文件。

这也是为什么推荐通过 CMake 开启 `CMAKE_EXPORT_COMPILE_COMMANDS=ON`。工具需要知道源码在项目里真实是怎么被编译的。

## 生成目录和缓存

默认输出目录：

```text
<project_root>/build/metacc_files/
  include/
    metacc_<owner>.h
  src/
    metacc_<owner>.c
```

默认缓存目录：

```text
<project_root>/build/.metacc/.cache/
```

缓存按编译单元保存，依赖文件的 `mtime` 和大小变化会触发失效。构建规则直接以真实生成的 `.c/.h` 文件作为输出，首次构建会产出文件，后续增量构建可以复用未变化的解析结果。

## 命令行使用

源码模式：

```bash
tools/metacc/venv/bin/python tools/metacc/metacc.py \
  -c build/compile_commands.json \
  -p . \
  -g build/metacc_files \
  -d build/.metacc/.cache \
  -j 4
```

发布包模式：

```bash
tools/metacc/release/metacc \
  -c build/compile_commands.json \
  -p . \
  -g build/metacc_files \
  -d build/.metacc/.cache \
  -j 4
```

参数说明：

- `-c, --compile-commands`：`compile_commands.json` 路径。不传时会尝试自动查找。
- `-p, --project-root`：项目根目录。工具只会收集该目录内的注解。
- `-g, --generated-root`：生成文件根目录，默认是 `project_root/build/metacc_files`。
- `-d, --cache-dir`：缓存目录，默认是 `project_root/build/.metacc/.cache`。
- `-j, --jobs`：解析进程数。传 `0` 表示使用 `os.cpu_count()`。

## 在 CMake 项目中集成

最关键的前提是打开编译数据库：

```cmake
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
```

然后把 `metacc.h` 所在目录加入 include path。源码运行时通常引用 `tools/metacc`，发布包运行时则引用 `release`，两种方式都保持头文件来源清晰。

一个简化版集成如下：

```cmake
set(METACC_DIR "${CMAKE_SOURCE_DIR}/tools/metacc")
set(METACC_OUT_DIR "${CMAKE_BINARY_DIR}/metacc_files")
set(METACC_CACHE_DIR "${CMAKE_BINARY_DIR}/.metacc/.cache")
set(METACC_PYTHON "${METACC_DIR}/venv/bin/python")
set(METACC_SCRIPT "${METACC_DIR}/metacc.py")

set(METACC_GENERATED
    "${METACC_OUT_DIR}/src/metacc_module_init.c"
    "${METACC_OUT_DIR}/include/metacc_module_init.h"
)

add_custom_command(
    OUTPUT ${METACC_GENERATED}
    COMMAND ${CMAKE_COMMAND} -E make_directory
            "${METACC_OUT_DIR}/src"
            "${METACC_OUT_DIR}/include"
            "${METACC_CACHE_DIR}"
    COMMAND ${CMAKE_COMMAND} -E env "PYTHONPATH=${METACC_DIR}"
            "${METACC_PYTHON}" "${METACC_SCRIPT}"
            -c "${CMAKE_BINARY_DIR}/compile_commands.json"
            -p "${CMAKE_SOURCE_DIR}"
            -g "${METACC_OUT_DIR}"
            -d "${METACC_CACHE_DIR}"
    DEPENDS "${CMAKE_BINARY_DIR}/compile_commands.json"
            "${METACC_SCRIPT}"
            "${METACC_DIR}/metacc.h"
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    VERBATIM
)

add_custom_target(metacc_codegen ALL DEPENDS ${METACC_GENERATED})

target_include_directories(app PRIVATE
    "${METACC_DIR}"
    "${METACC_OUT_DIR}/include"
)

target_sources(app PRIVATE
    "${METACC_OUT_DIR}/src/metacc_module_init.c"
)

add_dependencies(app metacc_codegen)
```

## 安装源码运行环境

如果直接从源码运行，需要 Python 绑定和 native `libclang`。

在 `tools/metacc` 下准备虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e .
```

`pyproject.toml` 里声明了：

```toml
dependencies = [
    "libclang>=18.0.0",
]
```

在上面的 CMake 示例里，脚本使用：

```text
tools/metacc/venv/bin/python
```

如果你在其它项目里复用，要么保持这个路径约定，要么在自己的 CMake 封装里改成你自己的 Python 路径。

## 打包发布

发布使用 `package.sh`：

```bash
cd tools/metacc
./package.sh
```

发布目录是单层平铺结构：

```text
tools/metacc/release/
  metacc
  metacc.h
  libclang.so
  *.so
```

发布包可以直接作为归档根目录使用：

- `metacc` 是命令行可执行文件。
- `metacc.h` 是业务代码需要包含的标记宏头文件。
- `libclang.so` 和可执行文件同级，发布时保持平铺结构即可。

打包后可以直接验证：

```bash
tools/metacc/release/metacc --help
```

## 常见问题

### 为什么没有生成我的条目

优先检查这几件事：

1. `METACC_TABLE_ITEM` 是否直接写在源码里，保持和示例一致的原生标记形式。
2. 包含该源码的编译单元是否出现在 `compile_commands.json` 中。
3. `--project-root` 是否设置正确。工具只收集 project root 内的注解。
4. 相关头文件能否通过 compile command 里的 `-I`、`-iquote`、`-isystem` 找到。
5. `METACC_TABLE_ITEM` 的第一个参数是否和 `METACC_TABLE` 的表名完全一致。

### 为什么报 undefined METACC_TABLE

说明某个 item 引用了不存在的表名。

例如：

```c
METACC_TABLE(item_t, my_table)
METACC_TABLE_ITEM(my_tabel, 1, 2, 3)  /* 拼写错了 */
```

工具会输出具体文件和行号。

### 为什么函数有 prototype，但链接失败

生成文件和业务 `.c` 是不同翻译单元。条目里引用的函数如果是 `static`，生成文件无法链接到它。

解决方法是让该函数具备外部链接：

```c
int my_callback(void);
```

或者把条目设计成引用可见对象，而不是引用 `static` 私有符号。

### 为什么首次构建会解析很多文件

首次构建没有缓存，`metacc` 需要扫描 `compile_commands.json` 中的源文件。之后缓存会按依赖文件的修改时间和大小判断是否复用。

当 `CACHE_VERSION`、工具脚本、编译参数或依赖文件变化时，对应缓存会失效，这是正常行为。

### 为什么不用 section/linker set

section/linker set 是另一种常见方案，但它依赖编译器和链接脚本约定，跨平台行为更难统一。`metacc` 生成普通 C 数组，调试时可以直接打开生成的 `.c` 文件看最终顺序，更适合需要强可读构建产物的 SDK。

## 适合使用的场景

`metacc` 适合这些场景：

- 表项分散在多个模块里，但运行时希望是一张静态数组。
- 项目已经使用 CMake，并能生成 `compile_commands.json`。
- 希望避免运行时注册机制。
- 希望生成物是普通 C 源码，方便审查、调试和发布。

## 许可证

`metacc` 使用 Apache License 2.0。详见 `LICENSE`。
