/**
 *******************************************************************************
 * @file   metacc.h
 * @author houyuwenE@outlook.com
 * @brief  Public METACC annotation macros and reflection base types.
 * @version 0.2
 * @date 2026-05-22
 ******************************************************************************
 * @attention
 *
 * Copyright (c) 2026 houyuwen.
 * SPDX-License-Identifier: Apache-2.0
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 ******************************************************************************
 */
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/* Includes ------------------------------------------------------------------*/
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/* Private define ------------------------------------------------------------*/
/* Private macros ------------------------------------------------------------*/
/* Private types -------------------------------------------------------------*/
/* Private constants ---------------------------------------------------------*/
/* Private variables ---------------------------------------------------------*/
/* Exported types ------------------------------------------------------------*/
/* Exported constants --------------------------------------------------------*/
/* Exported macro ------------------------------------------------------------*/
#define METACC_TABLE(type, name, ...)      /* metacc:table       */
#define METACC_TABLE_ITEM(name, ...)       /* metacc:table_item  */

/* Exported functions --------------------------------------------------------*/
#ifdef __cplusplus
}
#endif /* __cplusplus */

/* End of file ---------------------------------------------------------------*/
