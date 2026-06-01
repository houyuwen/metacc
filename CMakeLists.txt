# tools/metacc/CMakeLists.txt

# 安装 metacc.h 到 build/metacc_files/include 目录，便于主工程统一 include
install(
    FILES ${CMAKE_CURRENT_LIST_DIR}/metacc.h
    DESTINATION ${CMAKE_BINARY_DIR}/metacc_files/include
)
