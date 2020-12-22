# -*- coding: utf-8 -*-

"""

/***************************************************************************
 DemShading
                                 A QGIS plugin
 This plugin simulates natural shadows over an elevation model (DEM)
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2019-06-05
        copyright            : (C) 2019 by Zoran Čučković
        email                : cuckovic.zoran@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

__author__ = 'Zoran Čučković'
__date__ = '2019-06-05'
__copyright__ = '(C) 2019 by Zoran Čučković'

from os import sys

from PyQt5.QtCore import QCoreApplication
from qgis.core import (QgsProcessing,
                       QgsProcessingException,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterRasterDestination,
                        QgsProcessingParameterBoolean,
                      QgsProcessingParameterNumber,
                       QgsProcessingParameterEnum,

                       QgsProcessingUtils,
                       QgsRasterBandStats
                        )
from processing.core.ProcessingConfig import ProcessingConfig

import gdal
import numpy as np
from .modules.helpers import window_loop, filter3

class DemShadingAlgorithm(QgsProcessingAlgorithm):
    """
    This algorithm simulates natural shade over a raster DEM (in input). 
    """

    # Constants used to refer to parameters and outputs. They will be
    # used when calling the algorithm from another algorithm, or when
    # calling from the QGIS console.

    INPUT = 'INPUT'
    DIRECTION= 'DIRECTION'
    ANGLE= 'ANGLE'
    SMOOTH = 'SMOOTH'
   # ANALYSIS_TYPE='ANALYSIS_TYPE'
    OUTPUT = 'OUTPUT'

    ANALYSIS_TYPES = ['Depth', 'Reach']

    output_model = None #for post processing
    


    def initAlgorithm(self, config):
        """
        Here we define the inputs and output of the algorithm, along
        with some other properties.
        """

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT,
                self.tr('Digital elevation model')
            ) )
        
        self.addParameter(QgsProcessingParameterNumber(
            self.DIRECTION,
            self.tr('Sun direction (0 to 360°)'),
            1, 315, False, 0, 360))
            
        self.addParameter(QgsProcessingParameterNumber(
            self.ANGLE,
            self.tr('Sun angle'),
            1, 10, False, 0, 89))

        self.addParameter(QgsProcessingParameterBoolean(
            self.SMOOTH,
            self.tr('Smooth filter'),
            True, False)) 

        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
            self.tr("Shadow depth")))
        
    def processAlgorithm(self, parameters, context, feedback):
        """
        Here is where the processing itself takes place.
        """

        # 1) -------------- INPUT -----------------
        elevation_model= self.parameterAsRasterLayer(parameters,self.INPUT, context)


        if elevation_model.crs().mapUnits() != 0 :
            err= " \n ****** \n ERROR! \n Raster data should be projected in a metric system!"
            feedback.reportError(err, fatalError = True)
            raise QgsProcessingException(err)
        #could also use:
        #raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))

        if  round(abs(elevation_model.rasterUnitsPerPixelX()),
                    2) !=  round(abs(elevation_model.rasterUnitsPerPixelY()),2):
            
            err= (" \n ****** \n ERROR! \n Raster pixels are irregular in shape " +
                  "(probably due to incorrect projection)!")
            feedback.reportError(err, fatalError = True)
            raise QgsProcessingException(err)

        self.output_model = self.parameterAsOutputLayer(parameters,self.OUTPUT,context)

        direction = self.parameterAsDouble(parameters,self.DIRECTION, context)
        sun_angle =self.parameterAsDouble(parameters,self.ANGLE, context)
        
        smooth = self.parameterAsInt(parameters,self.SMOOTH, context)

        # 2)   --------------- ORIENTATION AND DIMENSIONS -----------------
        steep =  (45 <= direction <= 135 or 225 <= direction <= 315)

        s = direction % 90
        if s > 45: s= 90-s
                     
        slope = np.tan(np.radians(s ))# matrix shear slope
        tilt= np.tan(np.radians(sun_angle)) 

        dem = gdal.Open(elevation_model.source())       
            
        # ! attention: x in gdal is y dimension un numpy (the first dimension)
        xsize, ysize = dem.RasterXSize,dem.RasterYSize
        # assuming one band dem !
        nodata = dem.GetRasterBand(1).GetNoDataValue()
        
        pixel_size = dem.GetGeoTransform()[1]      
        
        chunk = int(ProcessingConfig.getSetting('DATA_CHUNK')) * 1000000
        chunk = min(chunk // (ysize if steep else xsize), (xsize if steep else ysize))

        if s%45 > 0 :
            # Determine the optimal chunk size (estimate!).
            # The problem is to carry rasterized lines 
            # from one chunk to another. 
            # So, set chunk size according to minimum rasterisation error
            c = (np.arange(1, chunk) * slope) % 1 # %1 to get decimals only
            c[c>0.5] -= 1
            # this is not ideal : we cannot predict where it would stop
            chunk -= np.argmin(np.round(abs(c), decimals = 2)[::-1])+1
   
        # writing output beforehand, to prepare for data dumps
        driver = gdal.GetDriverByName('GTiff')
        ds = driver.Create(self.output_model, xsize,ysize, 1, gdal.GDT_Float32)
        ds.SetProjection(dem.GetProjection())
        ds.SetGeoTransform(dem.GetGeoTransform())
        
        # 3) -------   SHEAR MATRIX (INDICES) -------------
        
        chunk_slice = (ysize, chunk) if steep else ( chunk, xsize)
        indices_y, indices_x = np.indices(chunk_slice)
        mx_z = np.zeros( chunk_slice); mx_z[:] = -99999
            
        # this is all upside down ...
        rev_y= 90 <= direction <= 270 
        rev_x= not 180 <= direction <= 360
        
        if rev_y: indices_y = indices_y[::-1,:]
        if not rev_x: indices_x = indices_x[:, ::-1]
        
        off_a = indices_x + indices_y * slope 
        off_b = indices_y + indices_x * slope 
        
        if steep:
            axis = 0
            # construct a slope to simulate sun angle  
            # elevations will be draped over this slope            
            off = off_a[:, ::-1]  
            
            src_y = indices_x [:,::-1]
            src_x = np.round(off_b).astype(int)          
            
        else:
            axis = 1
            off = off_b[:, ::-1]  

            src_x = indices_y
            src_y = np.round(off_a).astype(int)
          
        src = np.s_[src_y, src_x]        
        
        # x + y gives horizontal distance on x (!)
        # for orhtogonal distance to slope prependicular, 
        # we take cosine (given x+y is hypothenuse)
        off *= pixel_size * np.cos(np.radians(s)) * tilt
        
        # create a matrix to hold the sheared matrix 
        mx_temp = np.zeros(((np.max(src_y)+1), np.max(src_x)+1))
        
        t_y, t_x = mx_temp.shape

        # carrying lines from one chunk to the next (fussy...)
        if steep:        
            l = np.s_[-1  ,  : ysize ]
            f = np.s_[0  , t_x - ysize : ]            
        else:
            l = np.s_[t_y - xsize :  , -1 ]
            f = np.s_[:xsize, 0   ]
            
        last_line = np.zeros(( ysize if steep else xsize))
        
      # 4 -----   LOOP THOUGH DATA CHUNKS AND CALCULATE -----------------
        counter = 0   
        for dem_view, gdal_coords, unused_out, unused_gdal_out in window_loop ( 
            shape = (xsize, ysize), 
            chunk = chunk,
            axis = not steep, 
            reverse = rev_x if steep else rev_y,
            overlap= 0,
            offset = -1) :
      
            mx_z[dem_view]=dem.ReadAsArray(*gdal_coords).astype(float)
                
            # should handle better NoData !!
            # nans will destroy the accumulation sequence
            mask = mx_z == nodata
            mx_z[mask]= -9999        
       
            mx_temp[src] = mx_z + off
                        
            mx_temp[f] += -last_line # shadows have negative values, so *-1   
        
	    #accumulate maximum shadow depths
            mx_temp -= np.maximum.accumulate(mx_temp, axis= axis)
	
            # first line has shadow od zero depth (nothing to accum), so copy from previous chunk
            mx_temp[f] = last_line 
            
            last_line [:] = mx_temp[l ] # save for later

            out = mx_temp[src] 
            
            if smooth: out = filter3(out)
                
            out[mask]=np.nan 

            ds.GetRasterBand(1).WriteArray(out[dem_view], * gdal_coords[:2])

            counter += 1
            feedback.setProgress( 100 * chunk * counter / (xsize if steep else ysize))
            if feedback.isCanceled(): sys.exit()
           
               
        ds = None
        
        return {self.OUTPUT: self.output_model}

    def postProcessAlgorithm(self, context, feedback):
        
        import os
        output = QgsProcessingUtils.mapLayerFromString(self.output_model, context)
        
        provider = output.dataProvider()
                                                                               
        stats = provider.bandStatistics(1,QgsRasterBandStats.All,output.extent(),0)
        mean, sd = stats.mean, stats.stdDev
       # minv, maxv = stats.minimumValue, stats.maximumValue 

        if mean > -10: path= "/styles/shading_0-50.qml"
        elif mean < -30: path = "/styles/shading_0-500.qml"
        else:  path = "/styles/shading_0-250.qml" 

        path = os.path.dirname(__file__) + path

        output.loadNamedStyle(path)
        output.triggerRepaint()
        return {self.OUTPUT: self.output_model}

    def name(self):
        """
        Returns the algorithm name, used for identifying the algorithm. This
        string should be fixed for the algorithm, and must not be localised.
        The name should be unique within each provider. Names should contain
        lowercase alphanumeric characters only and no spaces or other
        formatting characters.
        """
        return 'Shadow depth'

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr(self.name())

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def shortHelpString(self):
        h = ( """
            <h3>    This algorithm models natural illumination over elevation models, namely shadows.
             
            <b>Input</b> should be an elevation model in raster format. The <b>output</b> will be smoothed where the value of each pixel is averaged with its neighbours whithin the specified radius (smooth radius). Values assigned to the output represent <b>shadow depth</b> below illuminated zones.
    
            <b>Sun direction</b> and <b>sun angle</b> parmeters define horizontal and vertical position of the sun, where 0° is on the North, 90° on the East and 270° on the West.

            For more information see: <a href="https://zoran-cuckovic.from.hr/QGIS-terrain-shading/">zoran-cuckovic.from.hr/QGIS-terrain-shading/</a>.
			
            Shading style definitions can be found in <a href="https://github.com/zoran-cuckovic/QGIS-terrain-shading/tree/styles">plugin repository</a>.   

	    If you find this plugin useful, please consider <a href = "https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=PM4YE7ZTPGLAU&source=url ">making a donation</a>.
			
            """)
		
        return self.tr(h)

    def createInstance(self):
        return DemShadingAlgorithm()
