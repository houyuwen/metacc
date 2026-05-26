# metacc 🚀

**基于 libclang 的静态元编程轻量化 C 语言代码生成工具链**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/Python-3.8%2B-green.svg)](https://www.python.org/)
[![Embedded](https://img.shields.io/badge/Embedded-MCU%20%7C%20RTOS-orange.svg)](https://en.wikipedia.org/wiki/Embedded_system)


`metacc` 是一款专为极致性能嵌入式固件、MCU SDK 及跨模块架构解耦设计的静态元编程工具。它利用 `libclang` 强大的 AST（抽象语法树）分析能力，在编译前置期自动提取 C 语言源文件中的原始静态标记 Token，全自动交叉汇编出强类型静态数据检索表。

运行时彻底消灭一切动态堆内存分配（`malloc`），核心检索算法全面压榨至二分效率，直击嵌入式第一性原理。

---

## 💡 设计哲学与核心限制

> [!IMPORTANT]
> **关键规范：必须直调原生 Token，不支持宏包装别名。**
> 由于本工具采用轻量级的静态语义提取，**不会像常规 C 预处理器那样执行宏展开**。这意味着在 C 语言中对原生宏进行二次包装（如 `#define REG_CMD(name) METACC_TABLE_ITEM(...)`）是**无法被工具识别的**。
>
> 任何开发人员编写业务 `.c` 代码时，必须彻底遵循**不加任何中间宏外壳、直接调用原生标记**的原则。

这种设计确保了 `metacc` 不需要考虑繁杂的 C 语言宏依赖拓扑，带来了 100% 的词法抓取确定性与极致紧凑的编译期安全。

---

## 📦 极简扁平化仓库结构


项目采用现代化轻量扁平布局（Modern Flat Layout），克隆后**原地不动、零污染引用**。

```text
metacc/
├── .gitignore          # 隔离缓存及编译中间件
├── LICENSE             # Apache-2.0 许可证
├── README.md           # 本说明文档
├── pyproject.toml      # 现代 Python 依赖锁定与全局 CLI 映射配置文件
├── metacc.py           # 宿主机工具：Python 核心解析与渲染脚本
└── metacc.h            # 目标机内核：C 语言原生 Token 声明头文件 (留在原地)
```


## 🛠️ 9大核心元编程运行时服务

通过在业务层直调原生的元编程 Token，工具链会自动生成对应的静态数据库。所有组件均已彻底解耦为独立的 .c/.h 模块：

| 编号 | 核心服务 | 模块原生 Token | 运行时检索算法与开销 |
|---|---|---|---|
| 1 | 自动初始化流水线 (Auto-Init) | METACC_TABLE / _ITEM | 编译期按字段优先级排序，运行时 O(1) 顺序遍历执行 |
| 2 | 强类型硬件设备表 (Device Table) | METACC_TABLE / _ITEM | 根据外设全局唯一 ID 进行 O(log N) 编译期二分检索排序 |
| 3 | 静态分布式事件总线 (Event Bus) | METACC_TABLE / _ITEM | 支持多路多订阅静态拓扑，索引表 O(log N) 路由分发 |
| 4 | 结构体字段反射 (Reflection) | METACC_STRUCT | 自动化计算字段名称、类型偏置及大小，免除手写反射对齐表 |
| 5 | 双向枚举字符串转换 (Enum Mapping) | METACC_ENUM | 自动生成 ToString 和 FromString 双向边界审计反射安全接口 |
| 6 | 零开销二进制序列化 (Serialize) | METACC_SERIALIZE | 针对特定结构体自动吐出零开销、免内存对齐冲突的 Pack/Unpack 静态流代码 |
| 7 | 哈希 Shell 命令行 (Hash Shell) | METACC_SHELL | 静态提取注册命令，参数类型自动安全转换，包装层 O(log N) 极速分发 |
| 8 | 解耦多态虚表 (Mock VTable Mux) | METACC_INTERFACE | 针对 C 抽象接口结构体一键吐出高保真单元测试 Mock 实体及调用计数器 |
| 9 | 编译期单向哈希 (FNV-1a Hash) | METACC_HASH | 在编译期全自动将指定明文字符串降维解算为固定 32 位整型哈希，斩断 RAM 字符串空间占用 |

## 🚀 宿主机快速开始 (Host Setup)

1. 依赖与一键本地安装

项目基于现代 Python 包装标准（PEP 517/621）。确保您的宿主机已正确安装系统级 LLVM 运行库。

在 metacc 仓库根目录下直接执行：

```bash
pip install -e .
```

该命令会自动利用 pyproject.toml 的声明拉取 Python clang 绑定，并在系统环境变量中无缝注册全局快捷命令 metacc。

## ⚓ 下游 C 工程“零污染”原地集成规范

严禁将 metacc.h 拷贝出仓库！请通过编译器的包含路径（-I）实现单源真理（Single Source of Truth）。

CMake 环境集成示范：在您的 MCU / 嵌入式大框架项目的 CMakeLists.txt 中引入以下全自动前置生成钩子：

```cmake
# 1. 指定全局元编程工具仓的相对克隆路径
set(METACC_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party/metacc)

# 2. 原地引入头文件路径 (绝不拷贝文件)
target_include_directories(${PROJECT_NAME} PRIVATE ${METACC_DIR})

# 3. 配置前置构建钩子：自动化扫描应用层代码并吐出中间件元数据
set(GENERATED_C_OUT ${CMAKE_CURRENT_BINARY_DIR}/metacc_out.c)

add_custom_command(
    OUTPUT ${GENERATED_C_OUT}
    COMMAND metacc --compile-commands ${CMAKE_BINARY_DIR}/compile_commands.json --generated-root ${CMAKE_CURRENT_BINARY_DIR}
    DEPENDS ${METACC_DIR}/metacc.py
    COMMENT "Metacc: Executing AST scanning and metadata generation..."
)

# 4. 将自动生成的实体文件追加至您的固件编译列表中
target_sources(${PROJECT_NAME} PRIVATE ${GENERATED_C_OUT} src/main.c)
```

---

📝 **应用层业务直调示范 (Code Examples)**

请直接在您的 .c 文件中尽情手写原生 Token。不需要任何前置宏别名包装：

**示例 1: 自动初始化流水线追加**

```c
void bsp_flash_init(void) {
    // 硬件底层 Flash 初始化逻辑
}
/* 告诉 metacc：将该函数丢入初始化流水线，优先级为 1，描述为 "flash_layer" */
METACC_TABLE_ITEM(MetaccInitTable, bsp_flash_init, 1, "flash_layer")
```

**示例 2: 零开销哈希 Shell 命令行注册**

```c
int cmd_reboot_handler(int argc, char *argv[]) {
    NVIC_SystemReset();
    return 0;
}
/* 注册全局命令 "reboot"，工具链自动包裹类型转换，生成帮助说明 */
METACC_SHELL("reboot", cmd_reboot_handler, "Reset MCU system safely")
```

**示例 3: 编译期 FNV-1a 单向字符串哈希**

```c
void log_message(uint32_t module_hash, const char* msg);

void app_process(void) {
    // 编译期全自动将 "SENSOR_MODULE" 静态解算为唯一 32 位整型数，不占用任何运行时 RAM 字符空间
    METACC_HASH(MODULE_HASH_VAL, "SENSOR_MODULE")
    log_message(MODULE_HASH_VAL, "Sensor data updated.");
}
```

---

📄 **开源许可证**

本项目基于 Apache License 2.0 许可证开源。详情请参阅 LICENSE 文件。